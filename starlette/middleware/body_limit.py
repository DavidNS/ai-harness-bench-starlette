from __future__ import annotations

import inspect
import sys

import anyio

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if sys.version_info < (3, 11):  # pragma: no cover
    try:
        from exceptiongroup import BaseExceptionGroup
    except ImportError:

        class BaseExceptionGroup(BaseException):  # type: ignore[no-redef]
            pass


BODY_LIMIT_SCOPE_KEY = "starlette.body_limit"


class _BodyTooLarge(BaseException):
    """
    Raised from `receive()` (or by the `Content-Length` check) when the limit is crossed.

    It derives from `BaseException` on purpose, so that it is not swallowed or turned into a
    500 response by `except Exception` blocks or by `ServerErrorMiddleware`. It is always
    caught by the layer that owns the limit, which responds with a 413.
    """


class _BodyLimitState:
    def __init__(self, scope: Scope, limit: int | None) -> None:
        self.limit = limit
        self.ceiling: int | None = None
        self.received = 0
        self.exceeded = False
        self.response_started = False
        self.checked = False
        declared = Headers(scope=scope).get("content-length")
        self.declared = int(declared) if declared is not None and declared.isascii() and declared.isdigit() else None

    def check_declared(self) -> None:
        """Fail fast when the declared `Content-Length` is over the limit that applies now."""
        self.checked = True
        if self.exceeded or (
            self.limit is not None and self.declared is not None and self.declared > 0 and self.declared > self.limit
        ):
            self.exceeded = True
            raise _BodyTooLarge()

    def set_limit(self, limit: int) -> None:
        """Replace the limit; bytes already received (and maybe buffered) count against the new one."""
        if self.ceiling is not None:
            limit = min(limit, self.ceiling)
        self.limit = limit
        if self.received > 0 and self.received > limit:
            self.exceeded = True
            raise _BodyTooLarge()

    def seal(self) -> None:
        """An app that can't be seen into was reached: its limit is a ceiling, inner limits may only lower it."""
        if self.limit is not None:  # pragma: no branch
            self.ceiling = self.limit if self.ceiling is None else min(self.ceiling, self.limit)

    def wrap_receive(self, receive: Receive) -> Receive:
        async def wrapped_receive() -> Message:
            if not self.checked:
                self.check_declared()
            if self.exceeded:
                raise _BodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if body:
                    self.received += len(body)
                    if self.limit is not None and self.received > self.limit:
                        self.exceeded = True
                        raise _BodyTooLarge()
            return message

        return wrapped_receive

    def wrap_send(self, send: Send) -> Send:
        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                if not self.checked:
                    # Nothing checked the declared length yet (e.g. an app that never reads the body).
                    self.check_declared()
                self.response_started = True
            await send(message)

        return wrapped_send


def may_change_limit(app: object) -> bool:
    """
    Whether `app` is, or is middleware (anything holding the next app as its `app` attribute)
    around, a router, route or Starlette application. Those may still raise or lower the body
    limit, so the declared `Content-Length` has to wait for them. Any other app is a leaf, and
    the limit that applies to it is already known.
    """
    from starlette.routing import BaseRoute, Router

    seen: set[int] = set()
    while id(app) not in seen:
        seen.add(id(app))
        if isinstance(app, (Router, BaseRoute)):
            return True
        try:
            if isinstance(getattr(app, "router", None), Router):
                return True
            inner = getattr(app, "app", None)
        except Exception:
            return False
        if inner is None or not callable(inner) or inspect.ismethod(inner):
            return False
        app = inner
    return False  # pragma: no cover


def _caused_by_limit(exc: BaseException) -> bool:
    return (
        isinstance(exc, (_BodyTooLarge, anyio.get_cancelled_exc_class()))
        # what `BaseHTTPMiddleware` raises when the app behind it was stopped before it responded
        or (isinstance(exc, RuntimeError) and str(exc) == "No response returned.")
    )


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    limit: int | None,
    check_content_length: bool,
) -> None:
    """
    Run `app` under a body limit.

    If a limit is already active for this request, `limit` (when not `None`) replaces it for the
    rest of the call tree, and the layer that owns the limit deals with an exceeded body.
    Otherwise, a `limit` that is not `None` starts a new one, owned by this call.

    `check_content_length` says whether the limit that applies is final, so a declared
    `Content-Length` over it is rejected right away.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimitState | None = scope.get(BODY_LIMIT_SCOPE_KEY)
    if state is not None:
        if limit is not None:
            state.set_limit(limit)
        if check_content_length:
            state.seal()
            state.check_declared()
        await app(scope, receive, send)
        return

    if limit is None:
        await app(scope, receive, send)
        return

    state = _BodyLimitState(scope, limit)
    scope[BODY_LIMIT_SCOPE_KEY] = state
    try:
        if check_content_length:
            state.seal()
            state.check_declared()
        await app(scope, state.wrap_receive(receive), state.wrap_send(send))
    except _BodyTooLarge:
        pass
    except BaseExceptionGroup as group:
        # Task groups (e.g. in `BaseHTTPMiddleware`) wrap the error, along with the cancellations it
        # caused. Anything else in the group is unrelated to the limit and must still surface.
        if not state.exceeded:
            raise
        _, unrelated = group.split(_caused_by_limit)
        if unrelated is not None:
            raise unrelated from None
    finally:
        scope.pop(BODY_LIMIT_SCOPE_KEY, None)

    if state.exceeded and not state.response_started:
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(scope, receive, send)


class RequestBodyLimitMiddleware:
    """
    Rejects HTTP requests whose body is larger than `max_body_size` bytes with a 413 response.

    The bytes counted are the ones received from the server. `Content-Length` is only used to
    reject early. `max_body_size=None` disables the limit.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        self.app = app
        self.max_body_size = max_body_size
        # A wrapped router may still change the limit, so then the header check waits for it.
        self._check_content_length = not may_change_limit(app)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(
            self.app,
            scope,
            receive,
            send,
            limit=self.max_body_size,
            check_content_length=self._check_content_length,
        )


class _ApplicationBodyLimitMiddleware(RequestBodyLimitMiddleware):
    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        super().__init__(app, max_body_size)
        # An inner router, mount or route may still change the limit, so the declared
        # `Content-Length` is checked once routing has decided.
        self._check_content_length = False
