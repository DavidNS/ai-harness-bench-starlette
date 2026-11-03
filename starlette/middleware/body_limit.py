from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_STATE_KEY = "starlette.body_limit"


def _validate(max_body_size: int | None) -> None:
    if max_body_size is not None and (
        not isinstance(max_body_size, int) or isinstance(max_body_size, bool) or max_body_size < 0
    ):
        raise ValueError("max_body_size must be a non-negative integer or None.")


def _declared_length(scope: Scope) -> int:
    declared = -1
    for key, value in scope.get("headers", ()):
        if key.lower() == b"content-length":
            try:
                declared = max(declared, int(value))
            except ValueError:
                pass
    return declared


async def _unused_receive() -> Message:  # pragma: no cover
    raise RuntimeError("receive is not available.")


class _BodyLimitState:
    """
    The limit of one request. Two layers share it:

    * the counting layer, outermost, counts every byte read through `receive` (also by middleware);
    * the response layer sits inside the middleware (around the router). It drops what the application
      sends once the limit is crossed, and sends the 413 as an ordinary response through the
      middleware stack, from its own task and never from inside `receive`.

    Failures that unwind past the response layer are answered by the counting layer, straight to the server.
    """

    def __init__(self, limit: int | None, send: Send) -> None:
        self.limit = limit
        # The limit of the outermost scope (the application's), which applies when nothing matches.
        self.base = limit
        self.received = 0
        self.exceeded = False
        self.app_started = False  # the application started its response (seen by the response layer)
        self.server_started = False  # a response has started at the server
        self.response_layer = False
        self.sent_413 = False
        # Where a 413 is sent: the send of the response layer, or until there is one, the server's.
        self.send = send

    async def send_413(self, send: Send | None = None) -> None:
        if self.sent_413:
            return
        self.sent_413 = True  # at most once: an interrupted send is never repeated
        response = PlainTextResponse("Content Too Large", status_code=413)
        send = send or self.send
        await send({"type": "http.response.start", "status": 413, "headers": response.raw_headers})
        await send({"type": "http.response.body", "body": response.body})

    async def reject_if_over(self, scope: Scope, limit: int | None) -> bool:
        """Fail-fast, from routing (or on entry): a 413 before any application code runs."""
        if limit is not None and (self.received > limit or _declared_length(scope) > limit):
            self.exceeded = True
        if not self.exceeded:
            return False
        if not self.app_started:
            await self.send_413()
        return True

    def count(self, receive: Receive) -> Receive:
        async def limited_receive() -> Message:
            if self.exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                self.received += len(message.get("body", b""))
                if self.limit is not None and self.received > self.limit:
                    # The 413 is sent by the response layer when the application unwinds.
                    self.exceeded = True
                    return {"type": "http.disconnect"}
            return message

        return limited_receive

    def track(self, send: Send) -> Send:
        async def tracking_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.server_started = True
            await send(message)

        return tracking_send

    async def respond(self, app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
        """The response layer. Whatever unwinds after the limit was crossed ends in a 413, unless a response started."""
        self.response_layer = True
        self.send = send

        async def filtered_send(message: Message) -> None:
            if self.exceeded and not self.app_started:
                return  # the 413 is the response
            if message["type"] == "http.response.start":
                self.app_started = True
            await send(message)

        try:
            await app(scope, receive, filtered_send)
        except Exception:
            if not self.exceeded or self.app_started:
                raise
        if self.exceeded and not self.app_started:
            await self.send_413()

    async def backstop(self, app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
        """The counting layer: whatever escapes the whole stack after the limit was crossed becomes a 413."""
        try:
            await app(scope, self.count(receive), self.track(send))
        except Exception:
            if not self.exceeded or self.server_started:
                raise
        if self.exceeded and not self.server_started:
            self.sent_413 = False
            await self.send_413(send)


async def run_with_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    base_is_own: bool = False,
) -> None:
    """
    Used by the routing classes. Joins the limit state of the enclosing application,
    router or mount if there is one (replacing its current limit, not the application's),
    or starts a new one. The `Content-Length` check is left to the routing decision points.
    `base_is_own` is set by the outermost application or router, whose limit is "the application's".
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return
    state: _BodyLimitState | None = scope.get(_STATE_KEY)
    if state is not None:
        if max_body_size is not None:
            state.limit = max_body_size
        await app(scope, receive, send)
        return
    state = _BodyLimitState(max_body_size, send)
    if not base_is_own:
        state.base = None
    scope[_STATE_KEY] = state
    await state.respond(app, scope, state.count(receive), send)


class _ResponseLayer:
    """Inside the application's middleware, around its router: see `_BodyLimitState`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        state: _BodyLimitState | None = scope.get(_STATE_KEY)
        if scope["type"] != "http" or state is None or state.response_layer:
            await self.app(scope, receive, send)
        else:
            await state.respond(self.app, scope, receive, send)


class _CountingLayer:
    """Outermost, inside `ServerErrorMiddleware`: counts every read of the body."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state: _BodyLimitState | None = scope.get(_STATE_KEY)
        if state is not None:  # an application mounted inside another one's limit
            state.limit = self.max_body_size
            await self.app(scope, receive, send)
            return
        state = _BodyLimitState(self.max_body_size, send)
        scope[_STATE_KEY] = state
        await state.backstop(self.app, scope, receive, send)


async def reject_if_declared_too_large(scope: Scope, *, application: bool = False) -> bool:
    """
    Called where routing has settled the limit. Returns True if a 413 ended the request.
    With `application`, nothing matched, so the application's limit applies.
    """
    state: _BodyLimitState | None = scope.get(_STATE_KEY)
    if state is None or scope["type"] != "http":
        return False
    return await state.reject_if_over(scope, state.base if application else state.limit)


class RequestBodyLimitMiddleware:
    """Responds with 413 to HTTP requests whose body exceeds `max_body_size` bytes."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        _validate(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The state is private: nothing below this middleware can change its limit.
        state = _BodyLimitState(self.max_body_size, send)
        if await state.reject_if_over(scope, state.limit):
            return
        await state.respond(self.app, scope, state.count(receive), send)
