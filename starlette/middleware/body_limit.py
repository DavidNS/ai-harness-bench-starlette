from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised from `receive()` when the request body exceeds the configured limit."""


class _BodyLimitState:
    def __init__(self) -> None:
        self.limit: int | None = None
        self.received = 0
        self.exceeded = False
        self.response_started = False


def _declared_length(scope: Scope) -> int:
    declared = 0
    for name, value in scope["headers"]:
        if name == b"content-length":
            text = value.decode("latin-1").strip()
            if text.isascii() and text.isdigit():
                declared = max(declared, int(text))
    return declared


async def _reject(state: _BodyLimitState, scope: Scope, receive: Receive, send: Send) -> None:
    state.exceeded = True
    if not state.response_started:
        state.response_started = True
        await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)


async def apply_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    limit: int | None,
    *,
    defer_content_length: bool = False,
) -> None:
    """Run `app` with a request body limit.

    The first call for a request installs the byte counter. Later (inner) calls
    replace the limit that applies. A `limit` of `None` keeps the current one.
    When `defer_content_length` is set, the `Content-Length` fail-fast check is
    left to an inner call, because the limit may still change.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    owner = state is None
    if state is None:
        if limit is None:
            await app(scope, receive, send)
            return
        state = scope[_SCOPE_KEY] = _BodyLimitState()
    if limit is not None:
        state.limit = limit

    if (
        not defer_content_length
        and state.limit is not None
        and (state.received > state.limit or _declared_length(scope) > state.limit)
    ):
        await _reject(state, scope, receive, send)
        return

    if not owner:
        await app(scope, receive, send)
        return

    async def limited_receive() -> Message:
        assert state is not None
        if state.exceeded:
            raise RequestBodyTooLarge()
        message = await receive()
        if message["type"] == "http.request":
            state.received += len(message.get("body", b""))
            if state.limit is not None and state.received > state.limit:
                state.exceeded = True
                raise RequestBodyTooLarge()
        return message

    async def tracking_send(message: Message) -> None:
        assert state is not None
        if message["type"] == "http.response.start":
            state.response_started = True
        await send(message)

    try:
        await app(scope, limited_receive, tracking_send)
    except RequestBodyTooLarge:
        pass
    except Exception:
        if not state.exceeded or state.response_started:
            raise
    if state.exceeded:
        await _reject(state, scope, receive, send)


class RequestBodyLimitMiddleware:
    """Rejects requests whose body is larger than `max_body_size` bytes with a 413 response."""

    _defer_content_length = False

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await apply_body_limit(
            self.app,
            scope,
            receive,
            send,
            self.max_body_size,
            defer_content_length=self._defer_content_length,
        )


class _ApplicationBodyLimitMiddleware(RequestBodyLimitMiddleware):
    # The routing layer settles the final limit, so it does the Content-Length check.
    _defer_content_length = True
