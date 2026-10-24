from __future__ import annotations

from starlette._body_limit import run_with_limit
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Reject HTTP requests whose body exceeds `max_body_size` bytes with a 413 response."""

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_limit(self.max_body_size, scope, receive, send, self.app, shared=False)
