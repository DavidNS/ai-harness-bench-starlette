from __future__ import annotations

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_SIZE_SCOPE_KEY = "starlette.max_body_size"
_STATE_SCOPE_KEY = "starlette.max_body_size_state"
_MISSING = object()


class _BodyState:
    def __init__(self) -> None:
        self.total = 0
        self.exceeded = False
        self.response_started = False


class RequestBodyLimitMiddleware:
    """
    Limit the total size of the request body, as received from the ASGI server.

    The effective limit lives in `scope[MAX_BODY_SIZE_SCOPE_KEY]`. The counting
    `receive` wrapper reads it dynamically, so a nested limited app only needs
    to swap the scope value to raise or lower the limit for its own scope.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: _BodyState | None = scope.get(_STATE_SCOPE_KEY)
        owner = state is None
        if state is None:
            state = _BodyState()
            scope[_STATE_SCOPE_KEY] = state
        previous = scope.get(MAX_BODY_SIZE_SCOPE_KEY, _MISSING)
        scope[MAX_BODY_SIZE_SCOPE_KEY] = self.max_body_size
        try:
            content_length = Headers(scope=scope).get("content-length")
            declared = int(content_length) if content_length is not None and content_length.isdigit() else 0
            too_large = declared > self.max_body_size
            if not owner:
                # Counting is already done by the outermost limited app.
                if too_large:
                    state.exceeded = True
                    raise HTTPException(status_code=413)
                await self.app(scope, receive, send)
                return

            if too_large:
                await self._reject(scope, receive, send)
                return

            async def limited_receive() -> Message:
                message = await receive()
                if message["type"] == "http.request":
                    state.total += len(message.get("body", b""))
                    if state.total > scope[MAX_BODY_SIZE_SCOPE_KEY]:
                        state.exceeded = True
                        raise HTTPException(status_code=413)
                return message

            async def tracking_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    state.response_started = True
                await send(message)

            try:
                await self.app(scope, limited_receive, tracking_send)
            except HTTPException as exc:
                if exc.status_code != 413 or not state.exceeded or state.response_started:
                    raise
                await self._reject(scope, receive, send)
        finally:
            if previous is _MISSING:
                del scope[MAX_BODY_SIZE_SCOPE_KEY]
            else:
                scope[MAX_BODY_SIZE_SCOPE_KEY] = previous
            if owner:
                del scope[_STATE_SCOPE_KEY]

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)
