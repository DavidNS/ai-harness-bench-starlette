from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LIMITER_SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised from `receive()` when the request body exceeds the configured limit."""

    def __init__(self, max_body_size: int) -> None:
        super().__init__(f"Request body exceeds the limit of {max_body_size} bytes.")
        self.max_body_size = max_body_size


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # An inner limiter replaces an outer one, so it can raise or lower the limit.
        outer = scope.get(_LIMITER_SCOPE_KEY)
        if outer is not None and outer[0] is receive:
            receive = outer[1]

        response = PlainTextResponse("Content Too Large", status_code=413)

        # Content-Length is only a fail-fast check, the received bytes are what is enforced.
        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None and content_length.isdigit() and int(content_length) > self.max_body_size:
            await response(scope, receive, send)
            return

        original_receive = receive
        max_body_size = self.max_body_size
        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            if received > max_body_size:
                raise RequestBodyTooLarge(max_body_size)
            message = await original_receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_body_size:
                    raise RequestBodyTooLarge(max_body_size)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        scope[_LIMITER_SCOPE_KEY] = (limited_receive, original_receive)
        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestBodyTooLarge:
            if not response_started:
                await response(scope, original_receive, send)
