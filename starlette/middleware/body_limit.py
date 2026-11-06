from __future__ import annotations

import math
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The per-request state is shared through the scope, so that the routing layer can
# override the limit of the part of the application it is currently dispatching to.
SCOPE_KEY = "starlette.body_limit"

# Python limits the number of digits `int()` accepts, a longer `Content-Length` is simply huge.
_MAX_CONTENT_LENGTH_DIGITS = 1000

# How deep to follow the `.app` attribute of wrapping middleware.
_MAX_WRAPPING_DEPTH = 64


class RequestBodyTooLarge(Exception):
    """Raised by `receive()` when the request body crosses the limit, and by every call after that."""


def validate_max_body_size(max_body_size: int | None) -> None:
    if max_body_size is None:
        return
    if not isinstance(max_body_size, int) or isinstance(max_body_size, bool):
        raise TypeError("max_body_size must be None or a number of bytes, as an int.")
    if max_body_size < 0:
        raise ValueError("max_body_size must be None or a non-negative number of bytes.")


def is_body_too_large(exc: BaseException) -> bool:
    # Tasks reading the body concurrently (a streaming response listening for a disconnect,
    # for instance) raise the error together, as an exception group.
    if isinstance(exc, RequestBodyTooLarge):
        return True
    group = getattr(exc, "exceptions", None)
    return isinstance(group, (tuple, list)) and bool(group) and all(is_body_too_large(inner) for inner in group)


class LimitSurvey:
    """The limits found in a tree of routes: `None` stands for no limit."""

    def __init__(self) -> None:
        self.limits: set[int | None] = set()
        self.explicit = False
        self.seen: set[tuple[int, int | None]] = set()

    @property
    def ceiling(self) -> int | None:
        """The largest body that any part of the tree accepts."""
        return None if None in self.limits else max(self.limits, default=None)  # type: ignore[type-var]


def survey_limits(app: Any, inherited: int | None, survey: LimitSurvey) -> None:
    """Add the limits that apply to every path through `app` to `survey`."""
    for _ in range(_MAX_WRAPPING_DEPTH):  # pragma: no branch
        key = (id(app), inherited)
        if key in survey.seen:
            # Applications can refer back to each other, once with a limit is enough.
            return
        survey.seen.add(key)
        hook = getattr(app, "_survey_body_limit", None)
        if hook is not None:
            hook(inherited, survey)
            return
        app = getattr(app, "app", None)
        if app is None:
            break
    survey.limits.add(inherited)  # pragma: no branch


def resolve_limit(app: Any, scope: Scope, inherited: int | None, depth: int) -> int | None:
    """The limit that applies to `scope` once routed through `app`, found without running anything."""
    if depth < _MAX_WRAPPING_DEPTH:  # pragma: no branch
        for _ in range(_MAX_WRAPPING_DEPTH):  # pragma: no branch
            hook = getattr(app, "_resolve_body_limit", None)
            if hook is not None:
                return hook(scope, inherited, depth + 1)  # type: ignore[no-any-return]
            app = getattr(app, "app", None)
            if app is None:
                break
    return inherited


def may_change_limit(app: Any) -> bool:
    """Whether `app` is, or wraps, a router or application that sets limits of its own."""
    for _ in range(_MAX_WRAPPING_DEPTH):
        if hasattr(app, "_survey_body_limit"):
            return True
        app = getattr(app, "app", None)
        if app is None:
            return False
    return True  # pragma: no cover


def declared_length(scope: Scope) -> float | None:
    """The `Content-Length` of the request, or `None` when it is absent or ambiguous."""
    values = set(Headers(scope=scope).getlist("content-length"))
    if len(values) != 1:
        # The received bytes are what is counted anyway.
        return None
    value = values.pop().strip()
    if not (value.isascii() and value.isdigit()):
        return None
    value = value.lstrip("0")
    return math.inf if len(value) > _MAX_CONTENT_LENGTH_DIGITS else int(value or "0")


class BodyLimit:
    """The limit, and the bytes counted against it, for a single HTTP request."""

    def __init__(self, scope: Scope, limit: int | None, nominal: int | None) -> None:
        # Not the scope itself: the state is stored in the scope, and a cycle would keep both alive.
        self.declared = declared_length(scope)
        # The limit enforced now. Until routing has decided, it is the largest limit of the tree.
        self.limit = limit
        # The limit that routes without a limit of their own inherit.
        self.nominal = nominal
        self.received = 0
        self.exceeded = False
        self.response_started = False
        # A `Content-Length` check that is left to the first read of the body.
        self.deferred = False
        # Routing picked a route, and nothing has applied the limit that is meant for it yet.
        self.route_pending = False

    def set_limit(self, limit: int | None) -> None:
        self.limit = limit
        if limit is not None and self.received > limit:
            self.exceeded = True

    def declared_too_large(self) -> bool:
        return self.limit is not None and self.declared is not None and self.declared > self.limit

    def check_declared(self) -> None:
        self.deferred = False
        if not self.exceeded and self.declared_too_large():
            self.exceeded = True

    def wrap(self, receive: Receive, send: Send) -> tuple[Receive, Send]:
        async def limited_receive() -> Message:
            if self.route_pending:
                # The route did not apply a limit of its own: it gets the one it inherits.
                self.route_pending = False
                self.set_limit(self.nominal)
                self.deferred = True
            if self.deferred:
                self.check_declared()
            if self.exceeded:
                raise RequestBodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":  # pragma: no branch
                self.received += len(message.get("body", b""))
                if self.limit is not None and self.received > self.limit:  # pragma: no branch
                    self.exceeded = True
                    raise RequestBodyTooLarge()
            return message

        async def limited_send(message: Message) -> None:
            if self.exceeded:
                # Whatever the application says after the limit was crossed is dropped: either it
                # is replaced by the 413 response, or the response it already started is cut off.
                return
            if message["type"] == "http.response.start":
                self.response_started = True
            await send(message)

        return limited_receive, limited_send

    async def reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.response_started:
            return
        self.response_started = True
        await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)

    async def run_owner(self, app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
        """Call `app` with the bytes counted. Only the owner sends the 413, with the server's `send`."""
        limited_receive, limited_send = self.wrap(receive, send)
        try:
            if not self.exceeded:
                await app(scope, limited_receive, limited_send)
        except Exception as exc:
            if not self.exceeded:
                raise
            await self.reject(scope, receive, send)
            if not is_body_too_large(exc):
                raise
            return
        if self.exceeded:
            await self.reject(scope, receive, send)


async def dispatch(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    final: bool,
    install: bool = False,
    ceiling: Any = None,
) -> None:
    """
    Call `app`, applying `max_body_size` (if set) to the request limit in the scope.

    The outermost caller with a limit, or with `install=True`, owns the request: it counts
    the body and sends the 413. Everyone else only adjusts the limit, and raises
    `RequestBodyTooLarge` towards the owner when the request can not be accepted.

    When `final` is set, nothing behind `app` can change the limit: it is enforced now, and a
    declared `Content-Length` over it is rejected without calling `app`. Otherwise the limit
    is only noted for the routes below, and the declared length is checked on the first read.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return
    state: BodyLimit | None = scope.get(SCOPE_KEY)
    owner = state is None
    if state is None:
        if max_body_size is None and not install:
            await app(scope, receive, send)
            return
        # Until routing has decided, the body may be as large as any part of the tree accepts.
        state = scope[SCOPE_KEY] = BodyLimit(
            scope, ceiling(scope) if ceiling is not None else max_body_size, max_body_size
        )
    elif max_body_size is not None:
        state.nominal = max_body_size
    state.route_pending = False
    if final:
        state.set_limit(state.nominal)
        state.check_declared()
    else:
        state.deferred = True
    if owner:
        await state.run_owner(app, scope, receive, send)
        return
    if state.exceeded:
        raise RequestBodyTooLarge()
    await app(scope, receive, send)


class RequestBodyLimitMiddleware:
    """
    Reject HTTP requests with a body of more than `max_body_size` bytes.

    The bytes counted are the ones received from the server, whatever `Content-Length` says.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return
        # The state is deliberately not published in the scope: this limit is a ceiling
        # that the routing layer of an application behind it can not override.
        state = BodyLimit(scope, self.max_body_size, self.max_body_size)
        state.check_declared()
        await state.run_owner(self.app, scope, receive, send)


def begin_route(scope: Scope, route: Any) -> None:
    """Called by routers before they hand an HTTP request to `route`."""
    state: BodyLimit | None = scope.get(SCOPE_KEY)
    if state is None:
        return
    if hasattr(route, "_survey_body_limit"):
        # It applies its own limit, or its subclass did not: then the first read does.
        state.route_pending = True
        return
    # A route this module knows nothing about gets the limit it inherits.
    state.set_limit(state.nominal)
    state.check_declared()
    if state.exceeded:
        raise RequestBodyTooLarge()
