from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_SIZE_SCOPE_KEY = "starlette.max_body_size"
"""ASGI scope key holding the effective body size limit while a limited app runs."""

_MISSING = object()


class RequestBodyLimitMiddleware:
    """
    Limits the total size of the HTTP request body.

    The body bytes received from the server are counted, so multipart data and
    encoding overhead are included. `Content-Length` is only used to fail fast.

    The limit is stored in the scope. When limited apps are nested, the innermost
    limit is the effective one, and the outermost middleware does the counting.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        previous = scope.get(MAX_BODY_SIZE_SCOPE_KEY, _MISSING)
        scope[MAX_BODY_SIZE_SCOPE_KEY] = self.max_body_size
        try:
            if previous is _MISSING:
                await self._counting_call(scope, receive, send)
            else:
                # An outer middleware is already counting, and reads the limit
                # from the scope, so only the effective limit changes.
                await self.app(scope, receive, send)
        finally:
            if previous is _MISSING:
                scope.pop(MAX_BODY_SIZE_SCOPE_KEY, None)
            else:
                scope[MAX_BODY_SIZE_SCOPE_KEY] = previous

    async def _counting_call(self, scope: Scope, receive: Receive, send: Send) -> None:
        declared: int | None = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break

        received = 0
        response_started = False
        too_large_sent = False

        def limit() -> int:
            return scope.get(MAX_BODY_SIZE_SCOPE_KEY, self.max_body_size)

        async def limited_receive() -> Message:
            nonlocal received
            if declared is not None and declared > limit():
                raise HTTPException(status_code=413)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit():
                    raise HTTPException(status_code=413)
            return message

        async def send_too_large() -> None:
            nonlocal too_large_sent, response_started
            too_large_sent = True
            response_started = True
            await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)

        async def limited_send(message: Message) -> None:
            nonlocal response_started
            if too_large_sent:
                return
            if message["type"] == "http.response.start" and declared is not None and declared > limit():
                # The declared length exceeds the (final) limit, even if the app never read the body.
                await send_too_large()
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except HTTPException as exc:
            if exc.status_code != 413 or response_started:
                raise
            await send_too_large()
