from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import Message, Receive, Scope, Send

SCOPE_KEY = "starlette.body_limit"


class BodyLimitState:
    """The limit in force for a request, shared by every layer that handles it."""

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False


class BodyTooLarge(Exception):
    def __init__(self, state: BodyLimitState) -> None:
        super().__init__("Request body too large")
        self.state = state


def _declared_length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _check_state(state: BodyLimitState, scope: Scope) -> None:
    if state.limit is None:
        return
    length = _declared_length(scope)
    if state.exceeded or (length is not None and length > state.limit):
        state.exceeded = True
        raise BodyTooLarge(state)


def check_content_length(scope: Scope) -> None:
    """Fail fast on a declared Content-Length, once the applicable limit is settled."""
    state = scope.get(SCOPE_KEY)
    if state is not None:
        _check_state(state, scope)


def defers_limit_check(app: Any) -> bool:
    """Whether `app` routes by itself and will settle the limit and check it later."""
    from starlette.routing import Router

    return isinstance(app, Router) or isinstance(getattr(app, "router", None), Router)


async def run_with_limit(
    limit: int | None,
    scope: Scope,
    receive: Receive,
    send: Send,
    call: Callable[[Scope, Receive, Send], Awaitable[None]],
    *,
    shared: bool = True,
) -> None:
    """
    Run `call` with a body limit in force.

    With `shared`, the limit is published in the scope so that inner routers, mounts and routes
    may override it; the outermost layer that sets a limit enforces it and sends the 413.
    """
    if scope["type"] != "http":
        await call(scope, receive, send)
        return

    existing: BodyLimitState | None = scope.get(SCOPE_KEY) if shared else None
    if existing is not None:
        if limit is not None:
            existing.limit = limit
        await call(scope, receive, send)
        return

    if limit is None:
        await call(scope, receive, send)
        return

    state = BodyLimitState(limit)
    response_started = False

    async def limited_receive() -> Message:
        if state.exceeded:
            raise BodyTooLarge(state)
        message = await receive()
        if message["type"] == "http.request" and state.limit is not None:
            body = message.get("body", b"")
            if state.received + len(body) > state.limit:
                state.exceeded = True
                state.received += len(body)
                # Hand over the chunk that crossed the limit but never report the
                # body as complete; the next read raises.
                return {**message, "more_body": True}
            state.received += len(body)
        return message

    async def tracking_send(message: Message) -> None:
        nonlocal response_started
        if message["type"] == "http.response.start":
            response_started = True
        await send(message)

    if shared:
        scope[SCOPE_KEY] = state
    try:
        if not shared:
            _check_state(state, scope)
        await call(scope, limited_receive, tracking_send)
    except BodyTooLarge as exc:
        if exc.state is not state:
            raise
        if not response_started:
            await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, tracking_send)
    finally:
        if shared:
            scope.pop(SCOPE_KEY, None)
