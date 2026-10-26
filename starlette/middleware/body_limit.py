from __future__ import annotations

import sys
from typing import NoReturn

from starlette.exceptions import RequestBodyTooLarge
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if sys.version_info < (3, 11):  # pragma: no cover
    from exceptiongroup import BaseExceptionGroup

__all__ = ["RequestBodyLimitMiddleware", "RequestBodyTooLarge"]

SCOPE_KEY = "starlette.body_limit"


def _caused_by(exc: BaseException, state: _BodyLimitState) -> bool:
    """Whether `exc` is the rejection of `state`, possibly wrapped in task group exception groups."""
    if isinstance(exc, BaseExceptionGroup):
        return all(_caused_by(inner, state) for inner in exc.exceptions)
    return isinstance(exc, RequestBodyTooLarge) and exc.state is state


def _validate(max_body_size: int | None) -> None:
    if max_body_size is None:
        return
    if not isinstance(max_body_size, int) or isinstance(max_body_size, bool):
        raise TypeError("max_body_size must be an integer or None.")
    if max_body_size < 0:
        raise ValueError("max_body_size must be a non-negative integer or None.")


def _declared_length(scope: Scope) -> int | None:
    lengths = [
        int(value) if len(value) <= 4000 else 1 << 4000  # Python refuses to parse very long integers
        for name, value in scope.get("headers", [])
        if name == b"content-length" and value.isdigit()
    ]
    return max(lengths, default=None)


class _BodyLimitState:
    """
    Per-request bookkeeping shared by every layer that sets a limit.

    The innermost layer decides the limit, except that a `fixed` layer (the standalone
    middleware) sets a ceiling that inner layers can lower but not raise.
    """

    def __init__(self, scope: Scope, limit: int, *, fixed: bool, receive: Receive | None = None) -> None:
        self.receive = receive
        self.limit = limit
        self.ceiling = limit if fixed else None
        self.received = 0
        self.declared = _declared_length(scope)
        self.exceeded = False
        self.response_started = False
        self.rejected = False

    def push(self, limit: int, *, fixed: bool) -> tuple[int, int | None]:
        previous = (self.limit, self.ceiling)
        if fixed:
            self.ceiling = limit if self.ceiling is None else min(self.ceiling, limit)
            limit = min(limit, self.limit)  # lowers an outer limit, never raises it
        self.limit = limit if self.ceiling is None else min(limit, self.ceiling)
        return previous

    def restore(self, previous: tuple[int, int | None]) -> None:
        self.limit, self.ceiling = previous

    def fail(self) -> NoReturn:
        self.exceeded = True
        raise RequestBodyTooLarge(self, self.receive)

    def settle(self, limit: int | None = None) -> None:
        """Reject now if the declared length, or what was already read, is over the limit."""
        limit = self.limit if limit is None else limit
        if self.exceeded or self.received > limit or (self.declared is not None and self.declared > limit):
            self.fail()


def settle_body_limit(scope: Scope) -> None:
    """Called once the limit that applies to a request is decided (e.g. routing finished)."""
    state: _BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is not None:
        state.settle()


class _BodyLimit:
    """
    Applies a body limit. If an outer layer already tracks the request, this only changes
    the limit for what is below it; otherwise it tracks the request itself and answers 413.

    `check_now` rejects on a too-large `Content-Length` immediately, rather than leaving
    that to whoever settles the limit later (the router, for a Starlette application).
    """

    def __init__(self, app: ASGIApp, max_body_size: int, *, check_now: bool, fixed: bool = False) -> None:
        _validate(max_body_size)
        self.app = app
        self.max_body_size = max_body_size
        self.check_now = check_now
        self.fixed = fixed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: _BodyLimitState | None = scope.get(SCOPE_KEY)
        if state is not None:
            previous = state.push(self.max_body_size, fixed=self.fixed)
            try:
                if self.check_now:
                    # A hard cap is checked on its own limit, an outer layer may have lowered the current one.
                    state.settle(self.max_body_size if self.fixed else None)
                await self.app(scope, receive, send)
            finally:
                state.restore(previous)
            return

        state = _BodyLimitState(scope, self.max_body_size, fixed=self.fixed, receive=receive)

        async def receive_wrapper() -> Message:
            if state.exceeded:
                state.fail()
            message = await receive()
            if message["type"] == "http.request":  # pragma: no branch
                body = message.get("body", b"")
                allowed = state.limit - state.received
                state.received += len(body)
                if state.received > state.limit:
                    if allowed <= 0:
                        state.fail()
                    # Hand over only the bytes that fit and promise more, so the next read fails. Whatever
                    # the app answers meanwhile is replaced by the 413.
                    state.exceeded = True
                    return {**message, "body": body[:allowed], "more_body": True}
            return message

        async def reject() -> None:
            state.rejected = True
            await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)

        async def send_wrapper(message: Message) -> None:
            if state.rejected:
                return
            if message["type"] == "http.response.start":
                if state.exceeded and not state.response_started:
                    state.response_started = True
                    await reject()
                    return
                state.response_started = True
            await send(message)

        scope[SCOPE_KEY] = state
        try:
            if self.check_now:
                state.settle()
            await self.app(scope, receive_wrapper, send_wrapper)
            if state.exceeded and not state.response_started:
                state.response_started = True
                await reject()
        except (RequestBodyTooLarge, BaseExceptionGroup) as exc:
            if not _caused_by(exc, state):
                raise
            if not state.response_started:
                state.response_started = True
                await reject()
        finally:
            scope.pop(SCOPE_KEY, None)


class RequestBodyLimitMiddleware(_BodyLimit):
    """
    Rejects HTTP requests whose body is larger than `max_body_size` bytes with a 413.

    The limit counts the body bytes received from the server. `Content-Length` is only
    used to fail fast, an absent or understated header does not let a larger body through.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        super().__init__(app, max_body_size, check_now=True, fixed=True)
