from __future__ import annotations

from typing import Any

import anyio

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SCOPE_KEY = "starlette.body_limiter"

_UNSET: Any = object()


def validate_max_body_size(max_body_size: int | None) -> int | None:
    if max_body_size is None:
        return None
    if isinstance(max_body_size, bool) or not isinstance(max_body_size, int):
        raise TypeError(f"max_body_size must be an int or None, not {type(max_body_size).__name__}.")
    if max_body_size < 0:
        raise ValueError("max_body_size must not be negative.")
    return max_body_size


class RequestBodyTooLarge(Exception):
    """Raised by the `receive` channel once the request body exceeds the limit."""

    def __init__(self, limiter: BodyLimiter) -> None:
        super().__init__("Request body exceeded the maximum size.")
        self.limiter = limiter


async def _unused_receive() -> Message:  # pragma: no cover
    raise RuntimeError("Receive channel is not available.")


class BodyLimiter:
    """
    Counts the body bytes received from the ASGI server, and enforces a limit on them.

    The limit may be changed while the request is being routed (see `Router`, `Mount`
    and `Route`), which is why it lives on this object rather than in a closure.
    `app_max_body_size` is the limit of the application itself, which applies to requests
    that match no route or no method.
    """

    def __init__(
        self, scope: Scope, receive: Receive, send: Send, max_body_size: int | None, app_max_body_size: int | None
    ) -> None:
        self.scope = scope
        self.max_body_size = max_body_size
        self.app_max_body_size = app_max_body_size
        self.received = 0
        self.exceeded = False
        self.response_started = False
        self.exceeded_event = anyio.Event()
        self._receive = receive
        self._send = send

    async def receive(self) -> Message:
        if self.exceeded:
            raise RequestBodyTooLarge(self)
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.max_body_size is not None and self.received > self.max_body_size:
                # The 413 is sent by whoever started the limiter, once the app has unwound.
                self.exceeded = True
                self.exceeded_event.set()
                raise RequestBodyTooLarge(self)
        return message

    async def send(self, message: Message) -> None:
        if self.exceeded:
            # The limit was crossed: the 413 is on its way, or the app's response is cut short.
            return
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)

    async def send_413(self, send: Send) -> None:
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(self.scope, _unused_receive, send)

    async def check_declared_length(self, limit: int | None, send: Send) -> bool:
        """
        Reject the request if it can already be told to be too large for `limit`.
        The 413 goes through `send`, the send of the layer that rejects, so that the layers
        around it see a response. Returns `True` if the request was rejected, in which case
        the app must not run.
        """
        if self.exceeded:
            return True
        if limit is None:
            return False
        if self.received > limit:
            await self.send_413(send)
            return True
        content_length = Headers(scope=self.scope).get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return False
            if declared > limit:
                await self.send_413(send)
                return True
        return False


def _is_limit_error(exc: BaseException, limiter: BodyLimiter) -> bool:
    if isinstance(exc, RequestBodyTooLarge):
        return exc.limiter is limiter
    # Raised inside a task group, the error arrives wrapped in an exception group.
    nested: Any = getattr(exc, "exceptions", None)
    return bool(nested) and all(_is_limit_error(e, limiter) for e in nested)


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    check_declared_length: bool = True,
    shared: bool = False,
    application: bool = False,
    declared_limit: Any = _UNSET,
) -> None:
    """
    Run `app` with a limit on the request body.

    With `shared=True` the limiter is published in the scope, so that inner routing
    layers can replace the limit. If a limiter is already published by an outer layer
    that one is reused, and its limit is replaced by `max_body_size` (unless that is `None`).
    `declared_limit` is the limit that a declared `Content-Length` is checked against, when
    it is not `max_body_size`.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    limiter: BodyLimiter | None = scope.get(SCOPE_KEY) if shared else None
    if declared_limit is _UNSET:
        declared_limit = max_body_size if max_body_size is not None or limiter is None else limiter.max_body_size

    if limiter is not None:
        if max_body_size is not None:
            limiter.max_body_size = max_body_size
        if check_declared_length and await limiter.check_declared_length(declared_limit, send):
            return
        await app(scope, receive, send)
        return

    if max_body_size is None:
        if check_declared_length and declared_limit is not None:
            rejecter = BodyLimiter(scope, receive, send, None, None)
            if await rejecter.check_declared_length(declared_limit, send):
                return
        await app(scope, receive, send)
        return

    app_max_body_size = max_body_size if application or "app" not in scope else None
    limiter = BodyLimiter(scope, receive, send, max_body_size, app_max_body_size)
    if shared:
        scope[SCOPE_KEY] = limiter
    if check_declared_length and await limiter.check_declared_length(declared_limit, send):
        return
    await _run_limited(app, scope, receive, send, limiter)


async def _run_limited(app: ASGIApp, scope: Scope, receive: Receive, send: Send, limiter: BodyLimiter) -> None:
    send_errors: list[Exception] = []

    async def respond_when_exceeded() -> None:
        # Runs next to the app rather than inside its `receive()`, so the 413 goes out as soon as
        # the limit is crossed whatever the app does next, and no cancel scope of the app applies
        # to it. The server's own cancellation still does.
        await limiter.exceeded_event.wait()
        if limiter.response_started:
            return
        try:
            await limiter.send_413(send)
        except Exception as exc:
            send_errors.append(exc)

    app_error: Exception | None = None
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(respond_when_exceeded)
        try:
            await app(scope, limiter.receive, limiter.send)
        except Exception as exc:
            app_error = exc
        if not limiter.exceeded:
            task_group.cancel_scope.cancel()
    if send_errors:
        raise send_errors[0]
    if app_error is not None:
        # Once the limit is crossed, what the app does next is a consequence of it. If it had
        # already started its response, only the limit's own error is dropped.
        if not limiter.exceeded or (limiter.response_started and not _is_limit_error(app_error, limiter)):
            raise app_error


async def check_routed_body_limit(scope: Scope, send: Send, *, unmatched: bool = False) -> bool:
    """
    Called once routing has decided which limit applies.
    With `unmatched` (no route or no method matched) the application's limit applies.
    Returns `True` if the request has been rejected.
    """
    limiter: BodyLimiter | None = scope.get(SCOPE_KEY)
    if limiter is None:
        return False
    if unmatched:
        limiter.max_body_size = limiter.app_max_body_size
    return await limiter.check_declared_length(limiter.max_body_size, send)


class RequestBodyLimitMiddleware:
    """
    Respond with `413 Content Too Large` to requests with a body larger than `max_body_size` bytes.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = validate_max_body_size(max_body_size)
        assert self.max_body_size is not None, "max_body_size is required."

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(self.app, scope, receive, send, self.max_body_size)
