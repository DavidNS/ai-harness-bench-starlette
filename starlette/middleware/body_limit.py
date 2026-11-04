from __future__ import annotations

from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

BODY_LIMIT_SCOPE_KEY = "starlette.body_limit"


def validate_max_body_size(max_body_size: int | None) -> int | None:
    if max_body_size is None:
        return None
    if isinstance(max_body_size, bool) or not isinstance(max_body_size, int):
        raise TypeError(f"max_body_size must be an int or None, not {max_body_size!r}")
    if max_body_size < 0:
        raise ValueError(f"max_body_size must not be negative, got {max_body_size}")
    return max_body_size


async def _no_receive() -> Message:  # pragma: no cover
    raise RuntimeError("receive is not available")


class BodyLimitState:
    """
    The per-request state shared by every layer that enforces a body limit.

    There is a single counter of the bytes received from the server, and a single
    current limit, which inner layers (routes, mounts, routers) may replace.
    """

    def __init__(self, scope: Scope, receive: Receive, send: Send, limit: int | None) -> None:
        self.scope = scope
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.response_started = False
        self._respond_due = False
        self._receive = receive
        self._send = send

    async def receive(self) -> Message:
        if self.exceeded:
            return {"type": "http.disconnect"}
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.limit is not None and self.received > self.limit:
                self.reject()
                return {"type": "http.disconnect"}
        return message

    async def send(self, message: Message) -> None:
        if self.exceeded and not self.response_started:
            # Our 413 is (being) sent, drop whatever the application sends afterwards.
            return
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)

    async def run(self, app: ASGIApp) -> None:
        try:
            await app(self.scope, self.receive, self.send)
        except Exception:
            if not self.exceeded:
                raise
            # The application only failed because its body stream was stopped.
        # The 413 is sent from here, outside every cancel scope of the application, so that
        # nothing the application does around `receive()` can cut it off, while a cancellation
        # from outside (a server timeout or shutdown) still reaches this task.
        await self.respond()

    def reject(self) -> None:
        """Stop the body stream. If no response has started yet, a 413 is due."""
        if self.exceeded:
            return
        self.exceeded = True
        self._respond_due = not self.response_started

    async def respond(self) -> None:
        if self._respond_due:
            self._respond_due = False
            await PlainTextResponse("Content Too Large", status_code=413)(self.scope, _no_receive, self._send)

    async def settle(self, resolve: Callable[[int], int] | None = None) -> bool:
        """
        Called once the limit that applies is known: fail fast on `Content-Length`.
        Returns `True` if the request was rejected, and the application must not run.

        `resolve` finds the limit that routing below will end up with, given the current one.
        It is only used when the declared length is over the current limit.
        """
        if self.exceeded:
            return True
        if self.limit is None:
            return False
        declared = _declared_length(self.scope)
        limit = self.limit
        if declared > limit and resolve is not None:
            # Only a prediction for the fail-fast check: the limit used for counting stays
            # the current one until the layer that handles the request sets its own.
            limit = resolve(limit)
        if self.received > self.limit or declared > limit:
            self.reject()
            await self.respond()
            return True
        return False


def _declared_length(scope: Scope) -> int:
    declared = 0
    for value in Headers(scope=scope).getlist("content-length"):
        try:
            declared = max(declared, int(value))
        except ValueError:
            pass
    return declared


async def settle_body_limit(scope: Scope) -> bool:
    """Fail fast on the current limit, if there is one. Returns `True` if the request was rejected."""
    state: BodyLimitState | None = scope.get(BODY_LIMIT_SCOPE_KEY)
    return state is not None and await state.settle()


async def call_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    settle: bool,
    resolve: Callable[[int], int] | None = None,
) -> None:
    """
    Call `app` with `max_body_size` applied to the request, as the limit that applies from here on.

    If a layer further out already counts the body, this replaces its limit.
    With `settle=True` the limit is final: a too large `Content-Length` is rejected right away.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: BodyLimitState | None = scope.get(BODY_LIMIT_SCOPE_KEY)
    if state is not None:
        if max_body_size is not None:
            state.limit = max_body_size
        if settle and await state.settle(resolve):
            return
        await app(scope, receive, send)
        return

    if max_body_size is None:
        await app(scope, receive, send)
        return

    state = BodyLimitState(scope, receive, send, max_body_size)
    scope[BODY_LIMIT_SCOPE_KEY] = state
    try:
        if settle and await state.settle(resolve):
            return
        await state.run(app)
    finally:
        scope.pop(BODY_LIMIT_SCOPE_KEY, None)


class RequestBodyLimitMiddleware:
    """
    Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413 response.

    The bytes received from the server are counted, `Content-Length` is only used to fail fast.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        self.app = app
        self.max_body_size = validate_max_body_size(max_body_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return
        # The limit of the middleware is its own: it counts the bytes it receives itself, so
        # nothing below it, e.g. a route with a higher `max_body_size`, can raise it.
        state = BodyLimitState(scope, receive, send, self.max_body_size)
        if await state.settle():
            return
        await state.run(self.app)
