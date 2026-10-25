from __future__ import annotations

from starlette._body_limit import run_with_body_limit
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """
    Rejects requests whose body is larger than `max_body_size` bytes with a 413 response.

    A declared `Content-Length` over the limit is rejected before the application runs.
    Otherwise the bytes received are counted, so an absent or understated header
    does not let a larger body through.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(self.app, scope, receive, send, None, settle=False, cap=self.max_body_size)
