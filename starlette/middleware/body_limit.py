from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_SIZE_SCOPE_KEY = "starlette.max_body_size"


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False
        exceeded = False
        total = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def receive_wrapper() -> Message:
            nonlocal total, exceeded
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_size:
                    exceeded = True
                    raise HTTPException(status_code=413, detail="Content Too Large")
            return message

        response = PlainTextResponse("Content Too Large", status_code=413)

        # Content-Length is only a fail-fast hint; the counting in receive is authoritative.
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self.max_body_size:
                    await response(scope, receive, send)
                    return
                break

        missing = object()
        previous = scope.get(MAX_BODY_SIZE_SCOPE_KEY, missing)
        scope[MAX_BODY_SIZE_SCOPE_KEY] = self.max_body_size
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except HTTPException:
            if not exceeded or response_started:
                raise
            await response(scope, receive, send)
        finally:
            if previous is missing:
                del scope[MAX_BODY_SIZE_SCOPE_KEY]
            else:
                scope[MAX_BODY_SIZE_SCOPE_KEY] = previous
