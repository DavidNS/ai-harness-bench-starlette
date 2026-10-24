from __future__ import annotations

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"
_DETAIL = "Content Too Large"


class RequestBodyTooLarge(HTTPException):
    """Raised from `receive()` once the request body exceeds the limit."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=_DETAIL)


class _BodyLimitState:
    """Per-request state, shared through the scope so that inner limits can override outer ones."""

    def __init__(self, limit: int, receive: Receive, send: Send) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.response_started = False
        self.discard_sends = False
        self._receive = receive
        self._send = send

    def declared_too_large(self, scope: Scope) -> bool:
        if self.received > self.limit:
            return True
        value = Headers(scope=scope).get("content-length")
        if value is None:
            return False
        try:
            return int(value) > self.limit
        except ValueError:
            # A malformed header cannot be trusted, the received bytes are still counted.
            return False

    async def receive(self) -> Message:
        if self.exceeded:
            raise RequestBodyTooLarge()
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.received > self.limit:
                self.exceeded = True
                if not self.response_started:
                    await self.respond(self._send)
                    self.discard_sends = True
                raise RequestBodyTooLarge()
        return message

    async def send(self, message: Message) -> None:
        if self.discard_sends:
            return
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)

    async def respond(self, send: Send) -> None:
        response = PlainTextResponse(_DETAIL, status_code=413)
        await response({"type": "http"}, _unused_receive, send)


async def _unused_receive() -> Message:  # pragma: no cover
    raise RuntimeError("The response does not read the request.")


async def reject_if_declared_too_large(scope: Scope, send: Send) -> bool:
    """
    Reject the request with a 413 if its `Content-Length` exceeds the limit in effect.

    Returns `True` if the response was sent, in which case the caller must not run the application.
    """
    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if scope["type"] == "http" and state is not None and state.declared_too_large(scope):
        await state.respond(send)
        return True
    return False


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    fail_fast: bool,
) -> None:
    """
    Run `app` with `max_body_size` applied to the request body.

    If a limit is already in effect for this request, `max_body_size` replaces it.
    With `fail_fast`, the declared `Content-Length` is checked against the limit in effect
    before `app` runs. Without it the caller is responsible for the check once the limit is settled.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is not None:
        if max_body_size is not None:
            state.limit = max_body_size
        if fail_fast and await reject_if_declared_too_large(scope, send):
            return
        await app(scope, receive, send)
        return

    if max_body_size is None:
        await app(scope, receive, send)
        return

    state = _BodyLimitState(max_body_size, receive, send)
    scope[_SCOPE_KEY] = state
    if fail_fast and await reject_if_declared_too_large(scope, send):
        return
    try:
        await app(scope, state.receive, state.send)
    except Exception:
        # The 413 has been dealt with, whatever the application made of the
        # exception (e.g. a 500 response, now discarded) is not interesting.
        if state.exceeded:
            return
        raise


class RequestBodyLimitMiddleware:
    """
    Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413 response.

    The bytes received from the server are counted, `Content-Length` is only used to fail fast.
    """

    _fail_fast = True

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        if max_body_size < 0:
            raise ValueError("max_body_size must not be negative.")
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(self.app, scope, receive, send, self.max_body_size, fail_fast=self._fail_fast)


class _DeferredRequestBodyLimitMiddleware(RequestBodyLimitMiddleware):
    """Used by `Starlette`: the `Content-Length` check waits until routing has settled the limit."""

    _fail_fast = False
