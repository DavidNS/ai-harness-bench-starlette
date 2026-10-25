from __future__ import annotations

from starlette._body_limit import BodyLimitState, run_limited, validate_max_body_size
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """
    Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413.

    Works with any ASGI application. A declared `Content-Length` over the limit is
    rejected on entry; the bytes actually received are counted as well. The limit is
    final: limits set further down (on a Starlette route, for instance) do not change it.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return
        state = BodyLimitState(scope, self.max_body_size)
        await run_limited(self.app, scope, receive, send, state, settle=True, shared=False)
