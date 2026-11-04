from __future__ import annotations

from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SCOPE_KEY = "starlette.body_limit"


class BodyTooLarge(Exception):
    """Raised from `receive()` once the request body has crossed the limit."""


def validate_max_body_size(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("max_body_size must be an integer or None.")
    if value < 0:
        raise ValueError("max_body_size must not be negative.")
    return value


def declared_over(scope: Scope, limit: int | None) -> bool:
    """Whether the `Content-Length` header declares more than `limit` bytes."""
    if limit is None:
        return False
    value = Headers(scope=scope).get("content-length")
    if value is None:
        return False
    try:
        return int(value) > limit
    except ValueError:
        return False


class BodyLimitState:
    """
    Shared by all the layers a request goes through. `limit` is set by applications, routers,
    mounts and routes, and the innermost one wins. `cap` is set by `RequestBodyLimitMiddleware`
    and can't be raised from below.
    """

    def __init__(self) -> None:
        self.limit: int | None = None
        self.cap: int | None = None
        self.received = 0
        self.exceeded = False
        self.response_started = False
        self.responded = False

    @property
    def effective(self) -> int | None:
        limits = [value for value in (self.limit, self.cap) if value is not None]
        return min(limits) if limits else None

    def over(self, scope: Scope) -> bool:
        effective = self.effective
        return effective is not None and (self.received > effective or declared_over(scope, effective))

    async def respond(self, scope: Scope, send: Send) -> None:
        if self.response_started or self.responded:
            return
        self.responded = True

        async def no_receive() -> Message:  # pragma: no cover
            raise RuntimeError("Receive channel has not been made available")

        await PlainTextResponse("Content Too Large", status_code=413)(scope, no_receive, send)


def defers_body_limit(app: Any) -> bool:
    """
    Whether `app` is a router or Starlette application, possibly behind middleware that exposes the wrapped
    app as `.app`, which makes the `Content-Length` check itself once its routing has settled the limit.
    """
    seen: set[int] = set()
    while app is not None and id(app) not in seen:
        seen.add(id(app))
        if getattr(app, "_defers_body_limit", False):
            return True
        app = getattr(app, "app", None)
    return False


def _is_body_too_large(exc: BaseException) -> bool:
    if isinstance(exc, BodyTooLarge):
        return True
    # Exception groups, as raised by task groups.
    children: Any = getattr(exc, "exceptions", None)
    return bool(children) and all(_is_body_too_large(child) for child in children)


async def check_body_limit(scope: Scope, send: Send) -> bool:
    """
    Fail fast on a declared `Content-Length` over the limit that currently applies.
    Returns `True` if the request was rejected, and the caller must not run any application code.
    """
    state: BodyLimitState | None = scope.get(SCOPE_KEY)
    if scope["type"] != "http" or state is None:
        return False
    if not state.over(scope):
        return False
    state.exceeded = True
    await state.respond(scope, send)
    return True


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    limit: int | None,
    *,
    fail_fast: bool,
    hard: bool = False,
    force: bool = False,
) -> None:
    """
    Run `app` under a body limit.

    A limit set by an inner layer replaces the one of the outer layers, unless it is `None`. A `hard` limit
    is a cap that inner layers can't raise. The layer that first sees the request owns the counting.

    Every layer turns the exception that stops the body into the 413 response, so that nothing in
    between (like an inner application's error handling) gets to see it.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: BodyLimitState | None = scope.get(SCOPE_KEY)
    owner = state is None
    if state is None:
        if limit is None and not force:
            await app(scope, receive, send)
            return
        state = BodyLimitState()
        scope[SCOPE_KEY] = state
        counted = state
        outer_receive, outer_send = receive, send

        async def receive_() -> Message:
            if counted.exceeded:
                raise BodyTooLarge()
            message = await outer_receive()
            if message["type"] == "http.request":
                counted.received += len(message.get("body", b""))
                effective = counted.effective
                if effective is not None and counted.received > effective:
                    counted.exceeded = True
                    raise BodyTooLarge()
            return message

        async def send_(message: Message) -> None:
            if message["type"] == "http.response.start":
                counted.response_started = True
            await outer_send(message)

        receive, send = receive_, send_

    if limit is not None:
        if hard:
            state.cap = limit if state.cap is None else min(state.cap, limit)
        else:
            state.limit = limit

    try:
        if fail_fast:
            if hard:
                over = declared_over(scope, limit)
                if over:
                    state.exceeded = True
                    await state.respond(scope, send)
                    return
            elif await check_body_limit(scope, send):
                return
        await app(scope, receive, send)
    except Exception as exc:
        if not (state.exceeded and _is_body_too_large(exc)):
            raise
    finally:
        if owner:
            scope.pop(SCOPE_KEY, None)
    if state.exceeded:
        await state.respond(scope, send)
