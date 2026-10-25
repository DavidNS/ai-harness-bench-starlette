from __future__ import annotations

import sys
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if sys.version_info < (3, 11):  # pragma: no cover
    from exceptiongroup import BaseExceptionGroup

SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised inside the application when the request body exceeds the applicable limit."""


def is_body_limit_error(exc: BaseException) -> bool:
    """Whether `exc` is a `RequestBodyTooLarge`, or a task group's group made only of those."""
    if isinstance(exc, RequestBodyTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(is_body_limit_error(sub) for sub in exc.exceptions)
    return False


class BodyLimitState:
    """
    Per-request state shared by every layer that sets a limit.

    The outermost layer installs it and counts the bytes received from the server.
    Routers, mounts, routes and applications replace `limit`, so the byte count is
    never reset and the limit can be raised or lowered for part of the tree.
    `RequestBodyLimitMiddleware` instead adds a cap, which nothing below it can raise.
    The limit in effect is the lowest of the caps and the limit.
    """

    def __init__(self, limit: int | None, receive: Receive) -> None:
        self.limit = limit
        # The limit of the outermost layer that set one: the application's.
        self.base_limit = limit
        self._base_set = limit is not None
        self.caps: list[int] = []
        self.received = 0
        self.exceeded = False
        self._receive = receive

    @property
    def effective(self) -> int | None:
        bounds = self.caps if self.limit is None else [*self.caps, self.limit]
        return min(bounds) if bounds else None

    def _fail(self) -> None:
        self.exceeded = True
        raise RequestBodyTooLarge()

    def _check_received(self) -> None:
        effective = self.effective
        if effective is not None and self.received > effective:
            self._fail()

    def set_limit(self, limit: int) -> None:
        if not self._base_set:
            self._base_set = True
            self.base_limit = limit
        self.limit = limit
        self._check_received()

    def add_cap(self, cap: int) -> None:
        self.caps.append(cap)
        self._check_received()

    def application_bound(self) -> int | None:
        bounds = self.caps if self.base_limit is None else [*self.caps, self.base_limit]
        return min(bounds) if bounds else None

    def check_declared_length(self, scope: Scope, bound: int | None = None) -> None:
        """Fail if a declared `Content-Length` is over `bound`, by default the limit in effect."""
        if bound is None:
            bound = self.effective
        if bound is None:
            return  # pragma: no cover
        for value in Headers(scope=scope).getlist("content-length"):
            try:
                declared = int(value)
            except ValueError:
                continue
            if declared > bound:
                self._fail()

    async def receive(self) -> Message:
        if self.exceeded:
            raise RequestBodyTooLarge()
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            self._check_received()
        return message


def settle_body_limit(scope: Scope) -> None:
    """Reject a declared `Content-Length` over the limit that now applies, if any."""
    state: BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is not None:
        state.check_declared_length(scope)


def settle_application_body_limit(scope: Scope) -> None:
    """
    Reject a declared `Content-Length` over the application's limit.

    For a request that matches no route, or no method: no route's limit applies,
    nor that of the mounts it was found under.
    """
    state: BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is not None:
        bound = state.application_bound()
        if bound is not None:
            state.check_declared_length(scope, bound)


def defers_body_limit(app: Any) -> bool:
    """Whether `app` settles the `Content-Length` check itself once it has routed."""
    # Look through wrappers, such as middleware, that hold the next app as `.app`.
    for _ in range(32):
        if getattr(app, "_settles_body_limit", False):
            return True
        app = getattr(app, "app", None)
        if app is None:
            break
    return False


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    limit: int | None,
    *,
    settle: bool = True,
    cap: int | None = None,
) -> None:
    """
    Call `app` with `limit` applied to the request body.

    `settle` controls whether a declared `Content-Length` is checked against the limit
    in effect right away, or left to something further down that will know the final one.
    `cap` is a ceiling that layers below cannot raise, which is what the standalone
    middleware needs. A declared length over a cap is always rejected right away.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is not None:
        # An outer layer owns the counting and the 413 response.
        if limit is not None:
            state.set_limit(limit)
        if cap is not None:
            state.check_declared_length(scope, cap)
            state.add_cap(cap)
        if settle:
            state.check_declared_length(scope)
        try:
            await app(scope, receive, send)
        finally:
            if cap is not None:
                state.caps.remove(cap)
        return

    if limit is None and cap is None:
        await app(scope, receive, send)
        return

    state = BodyLimitState(limit, receive)
    scope[SCOPE_KEY] = state
    response_started = False

    async def wrapped_send(message: Message) -> None:
        nonlocal response_started
        if state.exceeded:
            # Whatever the application sends once the limit is crossed is dropped,
            # so a 413 can still replace a response that has not started and a
            # response that has started is never completed.
            return
        if message["type"] == "http.response.start":
            response_started = True
        await send(message)

    try:
        if cap is not None:
            state.check_declared_length(scope, cap)
            state.add_cap(cap)
        if settle:
            state.check_declared_length(scope)
        await app(scope, state.receive, wrapped_send)
    except BaseException as exc:
        if not (state.exceeded and is_body_limit_error(exc)):
            raise
    finally:
        scope.pop(SCOPE_KEY, None)

    if state.exceeded and not response_started:
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(scope, receive, send)
