from __future__ import annotations

import anyio.lowlevel

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SCOPE_KEY = "starlette.body_limit"


class _BodyTooLarge(Exception):
    pass


def validate_max_body_size(value: object, *, allow_none: bool = True) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"max_body_size must be an int{' or None' if allow_none else ''}, not {value!r}")
    if value < 0:
        raise ValueError(f"max_body_size must not be negative, not {value!r}")


class _BodyLimit:
    """Per-request state: counts the received body and enforces the current limit."""

    def __init__(self, limit: int | None, base_limit: int | None, scope: Scope, receive: Receive, send: Send) -> None:
        self.limit = limit
        # The limit of the application, which applies to requests that no route handles.
        self.base_limit = base_limit
        self.scope, self.receive, self.send = scope, receive, send
        self.received = 0
        self.exceeded = False
        self.started = False
        self.replaced = False
        self.declared: int | None = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                try:
                    self.declared = int(value)
                except ValueError:
                    pass
                break

    def over(self) -> bool:
        """Whether the declared or already received size is over the current limit."""
        if self.limit is None:
            return False
        return (self.declared is not None and self.declared > self.limit) or self.received > self.limit

    def fail(self) -> _BodyTooLarge:
        self.exceeded = True
        return _BodyTooLarge()

    def wrap_receive(self, receive: Receive) -> Receive:
        async def limited_receive() -> Message:
            if self.exceeded:
                raise self.fail()
            message = await receive()
            if message["type"] == "http.request":
                self.received += len(message.get("body", b""))
                if self.limit is not None and self.received > self.limit:
                    # Let a response that another task is already sending go first: it started
                    # before the limit was crossed.
                    await anyio.lowlevel.checkpoint()
                    raise self.fail()
            return message

        return limited_receive

    def wrap_send(self, send: Send) -> Send:
        async def limited_send(message: Message) -> None:
            if self.replaced:
                return
            if message["type"] == "http.response.start":
                # A response that starts once the limit was crossed is replaced, whichever layer or
                # task sends it; one that started before the crossing is the application's own.
                if self.exceeded and not self.started:
                    await self.reject()
                    return
                self.started = True
            await send(message)

        return limited_send

    async def reject(self) -> None:
        self.exceeded = self.started = self.replaced = True
        await PlainTextResponse("Content Too Large", status_code=413)(self.scope, self.receive, self.send)

    async def run(self, app: ASGIApp) -> None:
        try:
            await app(self.scope, self.wrap_receive(self.receive), self.wrap_send(self.send))
        except Exception:
            if not self.exceeded:
                raise
        if self.exceeded and not self.started:
            await self.reject()


async def limited(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    application: bool = False,
) -> None:
    """Run `app` under `max_body_size`, for use by the routing classes.

    The limit is published in the scope so that inner routes, mounts and routers can override it;
    the `Content-Length` check is left to whoever settles the limit (see `check_body_limit`).
    """
    if scope["type"] != "http" or max_body_size is None:
        await app(scope, receive, send)
        return
    state: _BodyLimit | None = scope.get(SCOPE_KEY)
    if state is not None:
        state.limit = max_body_size
        await app(scope, receive, send)
        return
    state = scope[SCOPE_KEY] = _BodyLimit(max_body_size, max_body_size if application else None, scope, receive, send)
    try:
        await state.run(app)
    finally:
        scope.pop(SCOPE_KEY, None)


async def check_body_limit(scope: Scope, *, application: bool = False) -> bool:
    """Called once the limit that applies is settled; answers 413 to an oversized `Content-Length`.

    `application` settles on the limit of the application, for a request that no route handles.
    Returns whether the request was rejected, in which case the caller must not go on.
    """
    if scope["type"] == "http":
        state: _BodyLimit | None = scope.get(SCOPE_KEY)
        if state is not None:
            if application:
                state.limit = state.base_limit
            if state.over():
                await state.reject()
                return True
    return False


class _RoutingBodyLimitMiddleware:
    """The application-level limit of `Starlette(max_body_size=...)`."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await limited(self.app, scope, receive, send, self.max_body_size, application=True)


class RequestBodyLimitMiddleware:
    """Reject requests whose body is larger than `max_body_size` bytes with a 413 response."""

    def __init__(self, app: ASGIApp, max_body_size: int | None) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return

        state = _BodyLimit(self.max_body_size, self.max_body_size, scope, receive, send)
        if state.over():
            await state.reject()
            return
        await state.run(self.app)
