from __future__ import annotations

from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class _BodyLimitState:
    def __init__(self, scope: Scope, receive: Receive, send: Send, max_body_size: int | None) -> None:
        self.scope = scope
        self.raw_receive = receive
        self.raw_send = send
        self.max_body_size = max_body_size
        self.received = 0
        self.response_started = False
        self.exceeded = False

    def declared_too_large(self) -> bool:
        if self.max_body_size is None:
            return False
        content_length = Headers(scope=self.scope).get("content-length")
        return (
            content_length is not None
            and content_length.isascii()
            and content_length.isdigit()
            and int(content_length) > self.max_body_size
        )

    async def reject(self) -> None:
        self.exceeded = True
        if not self.response_started:
            self.response_started = True
            response = PlainTextResponse("Content Too Large", status_code=413)
            await response(self.scope, self.raw_receive, self.raw_send)

    async def receive(self) -> Message:
        if self.exceeded:
            return {"type": "http.disconnect"}
        if self.declared_too_large():
            # Checked when the body is first requested rather than up-front, so that
            # inner middleware instances have had the chance to change the limit.
            await self.reject()
            return {"type": "http.disconnect"}
        message = await self.raw_receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.max_body_size is not None and self.received > self.max_body_size:
                await self.reject()
                return {"type": "http.disconnect"}
        return message

    async def send(self, message: Message) -> None:
        if self.exceeded:
            return
        if message["type"] == "http.response.start":
            if self.declared_too_large():
                # The application never read the body; the limit is final now that it responds.
                await self.reject()
                return
            self.response_started = True
        await self.raw_send(message)


class RequestBodyLimitMiddleware:
    """
    Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413.

    A `max_body_size` of `None` means no limit. When several instances are nested,
    the innermost one that is reached sets the effective limit for the request.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
        owner = state is None
        if state is None:
            state = _BodyLimitState(scope, receive, send, self.max_body_size)
            scope[_SCOPE_KEY] = state
        else:
            state.max_body_size = self.max_body_size

        if not owner:
            if state.declared_too_large():
                await state.reject()
            else:
                await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, state.receive, state.send)
        except ClientDisconnect:
            if not state.exceeded:
                raise
