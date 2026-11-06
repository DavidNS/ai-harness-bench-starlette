from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised from `receive()` once a request body crosses the configured limit."""


def _only_too_large(exc: BaseException) -> bool:
    """True for `RequestBodyTooLarge`, also when it only arrives wrapped in exception groups."""
    nested = getattr(exc, "exceptions", None)
    if nested is not None and hasattr(exc, "split"):
        return len(nested) > 0 and all(_only_too_large(e) for e in nested)
    return isinstance(exc, RequestBodyTooLarge)


def decides_body_limit(app: Any) -> bool:
    """Whether routing below `app` will settle the limit (looking through `.app` wrappers)."""
    for _ in range(64):
        if getattr(app, "_decides_body_limit", False):
            return True
        inner = getattr(app, "app", None)
        if inner is None or inner is app:
            return False
        app = inner
    return False  # pragma: no cover


class _BodyLimit:
    """
    The per-request state shared by every layer that applies a body limit.

    * `hard`: set by `RequestBodyLimitMiddleware`; nothing further in can raise it.
    * `routed`: the limit routed layers have settled so far. It starts as the application's
      limit (`base`) and is replaced when routing commits to a route.
    * `pending`: the limit of the mounts and routers entered so far, not yet committed. It
      limits the body stream from the moment they are entered, so their own middleware
      is covered, but it only becomes the limit of a request once a route commits to it.
    """

    def __init__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.hard: int | None = None
        self.base: int | None = None
        self.routed: int | None = None
        self.pending: int | None = None
        self.received = 0
        self.exceeded = False
        self.started = False  # A response has been started on the real `send`.
        self.rejected = False  # The owner sent the 413 itself on the real `send`.
        self.scope = scope
        self.real_receive = receive
        self.real_send = send

    @staticmethod
    def _lowest(*limits: int | None) -> int | None:
        known = [limit for limit in limits if limit is not None]
        return min(known) if known else None

    @property
    def limit(self) -> int | None:
        """The limit on the body stream right now."""
        return self._lowest(self.hard, self.pending if self.pending is not None else self.routed)

    @property
    def settled_limit(self) -> int | None:
        """The limit routing has settled so far: no mount or router limit that is still pending."""
        return self._lowest(self.hard, self.routed)

    def set_hard(self, limit: int) -> None:
        self.hard = limit if self.hard is None else min(self.hard, limit)

    def set_base(self, limit: int) -> None:
        self.base = self.routed = limit
        self.pending = None

    def set_pending(self, limit: int | None) -> None:
        if limit is not None:
            self.pending = limit

    def commit(self, own: int | None) -> None:
        if own is not None:
            self.routed = own
        elif self.pending is not None:
            self.routed = self.pending
        self.pending = None

    async def receive(self) -> Message:
        if self.exceeded:
            raise RequestBodyTooLarge()
        message = await self.real_receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            limit = self.limit
            if limit is not None and self.received > limit:
                self.exceeded = True
                raise RequestBodyTooLarge()
        return message

    async def send(self, message: Message) -> None:
        if self.rejected:
            return
        if self.exceeded and not self.started:
            await self.reject()
            return
        if message["type"] == "http.response.start":
            self.started = True
        await self.real_send(message)

    async def reject(self) -> None:
        """The owner's 413, sent on the real `send` once the stream was stopped."""
        self.exceeded = True
        if self.started:
            return
        self.started = self.rejected = True
        await PlainTextResponse("Content Too Large", status_code=413)(self.scope, self.real_receive, self.real_send)

    async def reject_oversize(
        self, send: Send, *, declared: bool, hard_only: bool = False, settled: bool = False
    ) -> bool:
        """
        Send a 413 through `send`, the one of the layer that is deciding, if the request
        is over the limit that applies: bytes already received, or the declared length.
        """
        limit = self.hard if hard_only else self.settled_limit if settled else self.limit
        if limit is None:
            return False
        # With `settled`, bytes read inside a mount under its own limit still count against the
        # application's, since no route claimed the request.
        too_large = self.received > limit
        if not too_large and declared:
            content_length = Headers(scope=self.scope).get("content-length")
            try:
                too_large = content_length is not None and int(content_length) > limit
            except ValueError:
                pass
        if too_large and not self.started:
            await PlainTextResponse("Content Too Large", status_code=413)(self.scope, self.real_receive, send)
        return too_large

    async def run(self, app: ASGIApp) -> None:
        self.scope[_SCOPE_KEY] = self
        try:
            await app(self.scope, self.receive, self.send)
        except BaseException as exc:
            if self.exceeded:
                await self.reject()
                if _only_too_large(exc):
                    return
            raise
        finally:
            self.scope.pop(_SCOPE_KEY, None)
        if self.exceeded:
            await self.reject()


async def call_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    update: Callable[[_BodyLimit], None] | None,
    *,
    create: bool,
    check_declared: bool,
    hard_only: bool = False,
) -> None:
    """
    Call `app` for an HTTP request, after `update` changed the limits of the request.

    The outermost layer that applies a limit owns the request's state and wraps
    `receive` and `send`. Inner layers only change the state, and answer an oversized
    request through their own `send`. `create` says whether this layer starts the state
    when there is none yet.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimit | None = scope.get(_SCOPE_KEY)
    owner = state is None
    if state is None:
        if not create:
            await app(scope, receive, send)
            return
        state = _BodyLimit(scope, receive, send)
    if update is not None:
        update(state)
    if await state.reject_oversize(send, declared=check_declared, hard_only=hard_only):
        return
    if owner:
        await state.run(app)
    else:
        await app(scope, receive, send)


async def reject_if_declared_too_large(scope: Scope, send: Send) -> bool:
    """Used where routing found no route, or no method: the application's limit applies."""
    state: _BodyLimit | None = scope.get(_SCOPE_KEY)
    if state is None or scope["type"] != "http":
        return False
    return await state.reject_oversize(send, declared=True, settled=True)


class RequestBodyLimitMiddleware:
    """Responds with 413 to HTTP requests whose body is larger than `max_body_size` bytes."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await call_with_body_limit(
            self.app,
            scope,
            receive,
            send,
            lambda state: state.set_hard(self.max_body_size),
            create=True,
            check_declared=True,
            hard_only=True,
        )


class _RoutedRequestBodyLimitMiddleware:
    """
    Used by `Starlette` for its application-level limit. Routes below may still change
    the limit, so the declared `Content-Length` is checked once routing has decided.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await call_with_body_limit(
            self.app,
            scope,
            receive,
            send,
            lambda state: state.set_base(self.max_body_size),
            create=True,
            check_declared=False,
        )
