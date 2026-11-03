from __future__ import annotations

from typing import Literal

import anyio

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"
_ROOT_KEY = "starlette.body_limit.root"

# "now": check Content-Length against the limit right away.
# "defer": a mounted app may route again and change the limit, so the check happens as soon as a router settles
#   the limit, or at the latest when the mounted app first reads the request or sends anything.
# "none": the caller is followed by a router, which checks once it has routed.
Check = Literal["now", "defer", "none"]


def never_swallowed(exc: BaseException) -> bool:
    return isinstance(exc, (KeyboardInterrupt, SystemExit, anyio.get_cancelled_exc_class()))


class _BodyTooLarge(BaseException):
    """Raised inside the application when the request body exceeds the limit.

    A `BaseException`, so that neither application code nor middleware (error handlers, `BaseHTTPMiddleware`)
    mistakes crossing the limit for an error and answers with something else.
    """


class BodyLimitRoot:
    """Sees what really reaches the server, from above every middleware of a Starlette application.

    A limit layer may sit below middleware that holds back the response (like GZip) or completes it on its own
    (like BaseHTTPMiddleware), so it cannot tell by itself whether a response has started. If the body limit
    was crossed and nothing has reached the server yet, whatever response comes out is replaced by a 413.
    """

    def __init__(self, scope: Scope, receive: Receive) -> None:
        self.scope = scope
        self.receive = receive
        self.started = False
        self.exceeded = False
        self.replaced = False

    async def answer(self, send: Send) -> None:
        if not self.started:
            self.started = True
            await _send_too_large(self.scope, self.receive, send)

    def wrap_send(self, send: Send) -> Send:
        async def tracked(message: Message) -> None:
            if self.replaced:
                return
            if message["type"] == "http.response.start":
                if self.exceeded and not self.started:
                    self.started = self.replaced = True
                    await _send_too_large(self.scope, self.receive, send)
                    return
                self.started = True
            await send(message)

        return tracked


class _BodyLimitState:
    """Per-request state, shared through the scope by every layer that sets a limit."""

    def __init__(self, limit: int, root: BodyLimitRoot | None) -> None:
        self.root = root
        self.response_started = False
        self.limit = limit  # the limit of the innermost routing layer that set one
        self.ceiling: int | None = None  # set by standalone middleware; inner layers cannot raise it
        self.pending = False  # a Content-Length check is waiting for the limit to be settled
        self.received = 0
        self.body_complete = False
        self.exceeded = False

    @property
    def effective_limit(self) -> int:
        return self.limit if self.ceiling is None else min(self.limit, self.ceiling)

    def set_ceiling(self, limit: int) -> None:
        self.ceiling = limit if self.ceiling is None else min(self.ceiling, limit)

    def too_large(self, scope: Scope) -> bool:
        limit = self.effective_limit
        if self.received > limit:
            return True
        content_length = Headers(scope=scope).get("content-length")
        if content_length is None or not (content_length.isascii() and content_length.isdigit()):
            return False
        # Absurdly long digit strings are rejected by int(); they are certainly over the limit.
        return len(content_length) > 20 or int(content_length) > limit

    @property
    def can_still_respond(self) -> bool:
        started = self.root.started if self.root is not None else self.response_started
        return not started

    def mark_exceeded(self) -> None:
        self.exceeded = True
        if self.root is not None:
            self.root.exceeded = True

    def check_pending(self, scope: Scope) -> None:
        """Run a deferred check that no router settled before the mounted app first touched the request."""
        if self.pending:
            self.pending = False
            if self.too_large(scope):
                self.mark_exceeded()
                raise _BodyTooLarge()


async def _send_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)


async def reject_if_too_large(scope: Scope, receive: Receive, send: Send) -> bool:
    """Settle the limit: send a 413 and return True if the limit that currently applies is exceeded."""
    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is None:
        return False
    state.pending = False
    if state.too_large(scope):
        await _send_too_large(scope, receive, send)
        return True
    return False


async def run_with_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    limit: int | None,
    *,
    check: Check = "now",
    ceiling: bool = False,
) -> None:
    """Run `app` with `limit` as the body size limit that applies from here on.

    A limit set by a routing layer replaces the one already being enforced (`None` keeps it).
    With `ceiling=True` (standalone middleware) the limit is a hard cap that inner layers can only lower.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is not None:
        if limit is not None:
            if ceiling:
                state.set_ceiling(limit)
            else:
                state.limit = limit
        if check == "now":
            if await reject_if_too_large(scope, receive, send):
                return
        elif check == "defer":
            state.pending = True
        await app(scope, receive, send)
        return

    if limit is None:
        await app(scope, receive, send)
        return

    state = _BodyLimitState(limit, scope.get(_ROOT_KEY))
    if ceiling:
        state.set_ceiling(limit)
    scope[_SCOPE_KEY] = state

    async def limited_receive() -> Message:
        assert state is not None
        state.check_pending(scope)
        if state.exceeded:
            raise _BodyTooLarge()
        message = await receive()
        if message["type"] == "http.request":
            state.received += len(message.get("body", b""))
            if state.received > state.effective_limit:
                state.mark_exceeded()
                raise _BodyTooLarge()
            if not message.get("more_body", False):
                state.body_complete = True
        return message

    async def limited_send(message: Message) -> None:
        assert state is not None
        state.check_pending(scope)
        if message["type"] == "http.response.start":
            if state.exceeded and not state.response_started and state.can_still_respond:
                raise _BodyTooLarge()
            state.response_started = True
        await send(message)

    try:
        if check == "now" and await reject_if_too_large(scope, receive, send):
            return
        if check == "defer":
            state.pending = True
        try:
            await app(scope, limited_receive, limited_send)
        except BaseException as exc:
            if not state.exceeded or never_swallowed(exc):
                raise
        # Once a response has started no second one is sent; the receive side keeps failing, so the body stops.
        if state.exceeded and state.can_still_respond:
            if not state.response_started:
                await _send_too_large(scope, receive, send)
            else:  # a root exists, or the response could not have started
                # Something above holds back or completes the start this layer passed down, and nothing has
                # reached the server: a second start from here is not possible, so let the root answer.
                raise _BodyTooLarge()
    finally:
        scope.pop(_SCOPE_KEY, None)


class RequestBodyLimitMiddleware:
    """Rejects HTTP requests whose body is larger than `max_body_size` bytes with a 413 response."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_limit(self.app, scope, receive, send, self.max_body_size, ceiling=True)


class _AppBodyLimitMiddleware:
    """Application-level limit: the `Content-Length` check waits until routing has settled the limit."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_limit(self.app, scope, receive, send, self.max_body_size, check="none")
