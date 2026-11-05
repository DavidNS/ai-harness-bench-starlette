from __future__ import annotations

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SCOPE_KEY = "starlette.body_limit"

_BODY = b"Content Too Large"
_HEADERS = [
    (b"content-type", b"text/plain; charset=utf-8"),
    (b"content-length", str(len(_BODY)).encode("ascii")),
]


class BodyTooLarge(Exception):
    """Raised from `receive()` once the request body crosses the limit."""

    def __init__(self, state: BodyLimitState) -> None:
        super().__init__("Request body exceeds the maximum allowed size.")
        self.state = state


def _is_ours(exc: BaseException, state: BodyLimitState) -> bool:
    if isinstance(exc, BodyTooLarge):
        return exc.state is state
    nested = getattr(exc, "exceptions", None)  # (Base)ExceptionGroup, e.g. from a task group.
    if isinstance(nested, tuple) and nested:
        return all(_is_ours(e, state) for e in nested)
    return False


def validate_max_body_size(value: int | None) -> int | None:
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("max_body_size must be an int or None.")
        if value < 0:
            raise ValueError("max_body_size must not be negative.")
    return value


class BodyLimitState:
    """Per-request bookkeeping, shared with the routing layer through the scope."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.started = False  # A response has started (the application's or ours).

    def _declared_too_large(self, scope: Scope) -> bool:
        value = Headers(scope=scope).get("content-length")
        if value is None or not (value.isascii() and value.isdigit()):
            return False
        # Compare as strings: `int()` refuses very long digit strings.
        declared, limit = value.lstrip("0"), str(self.limit)
        return (len(declared), declared) > (len(limit), limit)

    async def send_too_large(self, send: Send) -> None:
        """
        Send the 413. Called before the application has run (fail-fast, through the send
        of the routing layer, so middleware in between sees an ordinary response), or from
        the frame that owns the limit once the application is done, with the raw send.
        It is never called from a task of the application, so the application can't
        cancel it half way through, and no application message can slip in between.
        """
        if self.started:
            return
        self.started = True
        await send({"type": "http.response.start", "status": 413, "headers": list(_HEADERS)})
        await send({"type": "http.response.body", "body": _BODY})

    async def reject_if_exceeded(self, scope: Scope, send: Send) -> bool:
        """Fail fast: respond with a 413 if the limit is already known to be crossed."""
        if self.exceeded or self.received > self.limit or self._declared_too_large(scope):
            self.exceeded = True
            await self.send_too_large(send)
            return True
        return False


async def reject_if_body_too_large(scope: Scope, send: Send) -> bool:
    """
    Called by the routing layer once it has settled which limit applies. Sends a
    `413` and returns `True` if the declared `Content-Length` is over that limit.
    """
    state = scope.get(SCOPE_KEY) if scope["type"] == "http" else None
    return state is not None and await state.reject_if_exceeded(scope, send)


async def call_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    settle: bool,
    expose: bool = True,
) -> None:
    """
    Run `app` under a body limit.

    With `expose`, the limit is stored in the scope so that inner routes, mounts and
    routers can override it. `settle` means nothing below `app` can change the limit
    any more, so the `Content-Length` check happens before `app` runs.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: BodyLimitState | None = scope.get(SCOPE_KEY) if expose else None
    if state is not None:
        if max_body_size is not None:
            state.limit = max_body_size
        if settle and await state.reject_if_exceeded(scope, send):
            return
        await app(scope, receive, send)
        return

    if max_body_size is None:
        await app(scope, receive, send)
        return

    state = BodyLimitState(max_body_size)
    if expose:
        scope[SCOPE_KEY] = state

    async def limited_receive() -> Message:
        if state.exceeded:
            raise BodyTooLarge(state)
        message = await receive()
        if message["type"] == "http.request":
            state.received += len(message.get("body", b""))
            if state.received > state.limit:
                state.exceeded = True
                raise BodyTooLarge(state)
        return message

    async def limited_send(message: Message) -> None:
        if state.exceeded and not state.started:
            return  # The application is answering a request we have cut off: the 413 replaces it.
        if message["type"] == "http.response.start":
            state.started = True
        await send(message)

    try:
        if not (settle and await state.reject_if_exceeded(scope, send)):
            await app(scope, limited_receive, limited_send)
    except BaseException as exc:
        if not state.exceeded:
            raise
        # Anything the application raises after we cut the request off is a consequence
        # of that (e.g. an `HTTPException` turned from a failed read); the answer is the
        # 413. Only an error after a response has started is still the application's.
        if not _is_ours(exc, state) and (state.started or not isinstance(exc, Exception)):
            raise  # pragma: no cover

    if state.exceeded:
        await state.send_too_large(send)


class RequestBodyLimitMiddleware:
    """
    Reject requests whose body is larger than `max_body_size` bytes with a `413`.

    Works with any ASGI application. A declared `Content-Length` over the limit is
    rejected before the application runs, and the bytes actually received are
    counted, so a missing or understated header doesn't let a larger body through.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        self.app = app
        self.max_body_size = validate_max_body_size(max_body_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await call_with_body_limit(self.app, scope, receive, send, self.max_body_size, settle=True, expose=False)


__all__ = ["RequestBodyLimitMiddleware"]
