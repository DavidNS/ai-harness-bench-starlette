from __future__ import annotations

from typing import Any

import anyio

from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limiter"


def check_max_body_size(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"max_body_size must be an int or None, not {type(value).__name__}.")
    if value < 0:
        raise ValueError("max_body_size must not be negative.")
    return value


class _BodyLimiter:
    """
    Per-request state: counts the body bytes received and enforces the limit that applies.

    The limit in effect is the matched route's own (`leaf`), else the one of the closest
    enclosing router or mount (`container`), else the application's (`base`).
    """

    def __init__(self, scope: Scope, receive: Receive, send: Send, base: int | None) -> None:
        self.scope = scope
        self._receive = receive
        self._send = send
        self.base = base
        self.container: int | None = None
        self.leaf: int | None = None
        self.received = 0
        self.exceeded = False
        self.started = False  # The application has started its own response.
        self.rejected = False  # We have sent the 413 response from `send`.
        self.writing = False  # The 413 is being written (or was) by the limiter's writer task.
        self._task_group: Any = None
        self._write_error: Exception | None = None
        self._written = anyio.Event()
        self.swallowed = False  # The application's first message was dropped in favour of the 413.
        self.own_response = False  # The 413 is being sent through `send` by the limiter's own caller.
        self.content_length: int | None = None
        value = Headers(scope=scope).get("content-length")
        if value is not None:
            try:
                self.content_length = int(value)
            except ValueError:
                pass

    @property
    def limit(self) -> int | None:
        for value in (self.leaf, self.container, self.base):
            if value is not None:
                return value
        return None  # pragma: no cover

    def recheck(self) -> None:
        limit = self.limit
        if limit is not None and self.received > limit:
            self.exceeded = True

    def declared_over(self, limit: int | None) -> bool:
        return limit is not None and self.content_length is not None and self.content_length > limit

    async def receive(self) -> Message:
        if not self.exceeded:
            message = await self._receive()
            if message["type"] != "http.request":
                return message
            self.received += len(message.get("body", b""))
            self.recheck()
            if not self.exceeded:
                return message
        # The application is only told the body stream is over once the 413 has been written, so that
        # whatever it does about the disconnect (cancel its tasks, return) cannot lose the response.
        await self.reject()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        if self.exceeded and not self.started and not self.own_response:
            if self.writing and (not self.rejected or self.swallowed):
                # The client is being or has been answered: tell the application, as a closed connection would.
                raise OSError("Request body too large")
            # The application's first message is replaced by the 413.
            await self.reject()
            self.swallowed = True
            return
        if message["type"] == "http.response.start":
            self.started = True
        await self._send(message)

    def begin_rejection(self) -> None:
        if not self.writing and not self.started:
            self.writing = True
            self._task_group.start_soon(self._write)

    async def reject(self) -> None:
        """
        Have the 413 written and wait for it. The writing is a task of the limiter, not of the caller,
        so the application cancelling its own tasks cannot interrupt it, a server cancelling the request can.
        """
        self.begin_rejection()
        if self.writing:
            await self._written.wait()

    async def _write(self) -> None:
        try:
            await _too_large(self.scope, self._send)
            self.rejected = True
        except Exception as exc:
            # Raised from `run()` as it is, not wrapped in the task group's exception group.
            self._write_error = exc
        finally:
            self._written.set()

    def settle_unmatched(self) -> bool:
        """Routing ended without a match: only the application's limit applies. Returns whether to reject."""
        self.container = None
        self.leaf = None
        self.recheck()
        return self.exceeded or self.declared_over(self.base)

    async def run(self, app: ASGIApp, scope: Scope) -> None:
        error: Exception | None = None
        async with anyio.create_task_group() as task_group:
            self._task_group = task_group
            try:
                await app(scope, self.receive, self.send)
            except Exception as exc:
                if not self.exceeded or (self.started and not isinstance(exc, (ClientDisconnect, OSError))):
                    error = exc
            if self.exceeded and error is None:
                self.begin_rejection()
            # Leaving the task group waits for the 413 to be written.
        if self._write_error is not None:
            raise self._write_error
        if error is not None:
            raise error


_TOO_LARGE_BODY = b"Content Too Large"
_TOO_LARGE_HEADERS = [
    (b"content-length", str(len(_TOO_LARGE_BODY)).encode()),
    (b"content-type", b"text/plain; charset=utf-8"),
]


async def _too_large(scope: Scope, send: Send) -> None:
    async def receive() -> Message:  # pragma: no cover
        raise RuntimeError("Receive channel is not available")

    await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)


async def call_with_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    limit: int | None,
    mode: str,
) -> None:
    """
    Call `app` with the body limit of this part of the routing tree applied.

    mode "base": the application's limit, applies to anything it routes to without a limit of its own.
    mode "container": a router or mount that goes on routing; its limit applies once a route matches.
    mode "leaf": routing has decided; a declared `Content-Length` over the limit is rejected before `app` runs.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    limiter: _BodyLimiter | None = scope.get(_SCOPE_KEY)
    owner = limiter is None
    if limiter is None:
        if limit is None:
            await app(scope, receive, send)
            return
        # A router that is the outermost thing of a plain ASGI setup is the application.
        is_base = mode == "base" or (mode == "router" and "app" not in scope)
        limiter = _BodyLimiter(scope, receive, send, limit if is_base else None)
    if mode == "base":
        # The limit of an application also replaces the one of the mount or router it is mounted in.
        limiter.base = limit
        limiter.container = None
        limiter.leaf = None
    elif mode in ("container", "router"):
        if limit is not None:
            limiter.container = limit
    else:
        limiter.leaf = limit
    limiter.recheck()
    if mode == "leaf" and (limiter.exceeded or limiter.declared_over(limiter.limit)):
        await _too_large(scope, send)
        return
    if not owner:
        await app(scope, receive, send)
        return
    scope[_SCOPE_KEY] = limiter
    try:
        await limiter.run(app, scope)
    finally:
        scope.pop(_SCOPE_KEY, None)


async def reject_if_too_large(scope: Scope, send: Send) -> bool:
    """Fail fast where routing ends without a match: only the application's limit applies."""
    limiter: _BodyLimiter | None = scope.get(_SCOPE_KEY) if scope["type"] == "http" else None
    if limiter is not None and limiter.settle_unmatched():
        if limiter.writing:
            await limiter.reject()  # Already being answered: wait for it, never answer twice.
            return True
        limiter.own_response = True
        try:
            await _too_large(scope, send)
        finally:
            limiter.own_response = False
        limiter.rejected = True
        return True
    return False


class _RoutingBodyLimit:
    """Applies a limit that inner routes, mounts and routers may still override."""

    def __init__(self, app: ASGIApp, max_body_size: int, mode: str) -> None:
        self.app = app
        self.max_body_size = max_body_size
        self.mode = mode

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await call_with_limit(self.app, scope, receive, send, self.max_body_size, self.mode)


def is_routed(app: ASGIApp) -> bool:
    """Whether `app`, possibly wrapped in middleware, goes on to route the request itself."""
    from starlette.applications import Starlette
    from starlette.routing import Router

    for _ in range(100):
        if isinstance(app, (Router, Starlette)):
            return True
        inner = getattr(app, "app", None)
        if inner is None:
            return False
        app = inner
    return False  # pragma: no cover


class RequestBodyLimitMiddleware:
    """
    Rejects requests whose body is larger than `max_body_size` bytes with a 413 response.

    The bytes received from the server are counted, `Content-Length` is only used to fail fast.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        check_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limiter = _BodyLimiter(scope, receive, send, self.max_body_size)
        if limiter.declared_over(limiter.limit):
            await _too_large(scope, send)
            return
        await limiter.run(self.app, scope)
