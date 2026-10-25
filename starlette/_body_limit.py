from __future__ import annotations

import functools
import inspect
import sys
import types

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if sys.version_info < (3, 11):  # pragma: no cover
    from exceptiongroup import BaseExceptionGroup

SCOPE_KEY = "starlette.body_limit"

# A Content-Length with more digits than this is larger than any limit we could be given.
_MAX_DECLARED_DIGITS = 18


class BodyTooLarge(BaseException):
    """
    Raised to abort the application once the body limit is crossed.

    This deliberately derives from `BaseException`, so that application code, exception
    handlers and error middleware (which handle `Exception`) cannot swallow it.
    """


def is_body_too_large(exc: BaseException) -> bool:
    if isinstance(exc, BodyTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(is_body_too_large(e) for e in exc.exceptions)
    return False


class BodyLimitState:
    """The state of one limiter: the limit that applies, and what has been received so far."""

    def __init__(self, scope: Scope, limit: int | None) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.stopped = False
        self.response_started = False
        self.declared: int | None = None
        value = Headers(scope=scope).get("content-length")
        if value is not None and value.isascii() and value.isdigit():
            digits = value.lstrip("0")
            self.declared = sys.maxsize if len(digits) > _MAX_DECLARED_DIGITS else int(digits or "0")

    def _over(self, declared: bool = True) -> bool:
        if self.limit is None:
            return False
        if self.received > self.limit:
            return True
        return declared and self.declared is not None and self.declared > self.limit

    def check(self, declared: bool = True) -> None:
        """Fail fast: abort if the declared Content-Length or the bytes so far exceed the limit."""
        if self._over(declared):
            self.exceeded = True
            raise BodyTooLarge()

    def set_limit(self, limit: int | None, settle: bool) -> None:
        if limit is not None:
            self.limit = limit
        # Bytes counted under the previous limit may already be over a lowered one.
        self.check(declared=settle)

    def _cross(self) -> Message:
        self.exceeded = True
        if not self.response_started:
            raise BodyTooLarge()
        # Too late for a 413: stop the body stream, and leave the response alone.
        self.stopped = True
        return {"type": "http.request", "body": b"", "more_body": False}

    def wrap_receive(self, receive: Receive) -> Receive:
        async def limited_receive() -> Message:
            if self.stopped:
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        return message
            if self.exceeded:
                raise BodyTooLarge()
            if self._over():
                return self._cross()
            message = await receive()
            if message["type"] == "http.request":
                self.received += len(message.get("body", b""))
                if self._over(declared=False):
                    return self._cross()
            return message

        return limited_receive

    def wrap_send(self, send: Send) -> Send:
        async def limited_send(message: Message) -> None:
            if self.exceeded and not self.response_started:
                return  # A 413 is sent instead.
            if message["type"] == "http.response.start":
                self.response_started = True
            await send(message)

        return limited_send


async def run_limited(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    state: BodyLimitState,
    *,
    settle: bool,
    shared: bool,
) -> None:
    if shared:
        scope[SCOPE_KEY] = state
    try:
        if settle:
            state.check()
        await app(scope, state.wrap_receive(receive), state.wrap_send(send))
    except BaseException as exc:
        if not state.exceeded:
            raise
        # The oversized body is the root cause, even if the application turned the
        # abort into another error. Once a response has started there is nothing to send.
        if not is_body_too_large(exc) and (state.response_started or not isinstance(exc, Exception)):
            raise
    finally:
        if shared:
            scope.pop(SCOPE_KEY, None)
    if state.exceeded and not state.response_started:
        await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)


async def apply_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    settle: bool = True,
) -> None:
    """
    Run `app` with `max_body_size` as the limit that applies from here on.

    The state is shared through the scope: an inner layer that sets a limit overrides the
    outer one. With `settle=True` a declared Content-Length is checked right away; use
    `settle=False` when something further down (a router) will do that once it knows
    which limit applies.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is not None:
        state.set_limit(max_body_size, settle)
        await app(scope, receive, send)
    elif max_body_size is None:
        await app(scope, receive, send)
    else:
        await run_limited(app, scope, receive, send, BodyLimitState(scope, max_body_size), settle=settle, shared=True)


def settle_body_limit(scope: Scope) -> None:
    state: BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is not None and scope["type"] == "http":
        state.check()


def reaches_router(app: object) -> bool:
    """
    Whether a router (which settles the limit itself) can be found under `app`.

    Wrappers are looked through by their attributes, wherever they keep the wrapped app,
    and by the cells of their closure.
    """
    from starlette.applications import Starlette
    from starlette.routing import Router

    seen: set[int] = set()
    pending: list[object] = [app]
    while pending and len(seen) < 256:
        obj = pending.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, (Router, Starlette)):
            return True
        if isinstance(obj, (list, tuple)):
            pending.extend(obj)
        elif isinstance(obj, functools.partial):
            pending.append(obj.func)
        elif inspect.isfunction(obj):
            pending.extend(_cell_contents(obj))
        elif hasattr(obj, "__dict__") and not isinstance(obj, (type, types.ModuleType)):
            pending.extend(vars(obj).values())
            for cell in _cell_contents(getattr(obj, "__call__", None)):
                pending.append(cell)
    return False


def _cell_contents(func: object) -> list[object]:
    contents: list[object] = []
    for cell in getattr(func, "__closure__", None) or ():
        try:
            contents.append(cell.cell_contents)
        except ValueError:  # pragma: no cover
            pass
    return contents


def validate_max_body_size(max_body_size: int | None) -> None:
    if max_body_size is not None and (
        not isinstance(max_body_size, int) or isinstance(max_body_size, bool) or max_body_size < 0
    ):
        raise ValueError("max_body_size must be a non-negative integer or None.")
