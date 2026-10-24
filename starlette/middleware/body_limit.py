from __future__ import annotations

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Content Too Large")


class _BodyLimitState:
    """Per-request state shared by every body limit that applies to a request."""

    def __init__(self, receive: Receive, send: Send) -> None:
        self.limit: int | None = None
        self.app_limit: int | None = None  # the outermost limit, for requests no route handles
        self.received = 0
        self.exceeded = False
        self.response_started = False
        self._receive = receive
        self._send = send

    async def receive(self) -> Message:
        if self.exceeded and self.response_started:
            return {"type": "http.disconnect"}
        if self.exceeded:
            raise RequestBodyTooLarge()
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.limit is not None and self.received > self.limit:
                self.exceeded = True
                if self.response_started:
                    # Too late for a 413: stop the body stream instead.
                    return {"type": "http.disconnect"}
                raise RequestBodyTooLarge()
        return message

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)

    def declared_too_large(self, scope: Scope) -> bool:
        if self.limit is None:
            return False
        value = Headers(scope=scope).get("content-length")
        if value is None:
            return False
        try:
            return int(value) > self.limit
        except ValueError:
            return False


async def _send_413(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)


async def reject_if_declared_too_large(scope: Scope, receive: Receive, send: Send, *, unrouted: bool = False) -> bool:
    """Send a 413 if the declared `Content-Length` exceeds the limit that currently applies.

    With `unrouted`, the request matched no route or method, so the outermost limit applies.
    """
    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is not None and unrouted:
        state.limit = state.app_limit
    if state is not None and scope["type"] == "http" and state.declared_too_large(scope):
        state.exceeded = True
        await _send_413(scope, receive, send)
        return True
    return False


class RequestBodyLimitMiddleware:
    """Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413."""

    def __init__(self, app: ASGIApp, max_body_size: int | None = None, *, _check_content_length: bool = True) -> None:
        self.app = app
        self.max_body_size = max_body_size
        self._check_content_length = _check_content_length

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (self.max_body_size is None and not self._check_content_length):
            await self.app(scope, receive, send)
            return

        state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
        owner = state is None
        if state is None:
            state = _BodyLimitState(receive, send)
            state.app_limit = self.max_body_size
            scope[_SCOPE_KEY] = state
        if self.max_body_size is not None:
            state.limit = self.max_body_size

        if self._check_content_length and state.declared_too_large(scope):
            state.exceeded = True
            await _send_413(scope, receive, send)
            return

        if not owner:
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, state.receive, state.send)
        except RequestBodyTooLarge:
            if not state.response_started:
                await _send_413(scope, receive, send)
        else:
            if state.exceeded and not state.response_started:
                await _send_413(scope, receive, send)
