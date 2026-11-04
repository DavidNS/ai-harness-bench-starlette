from __future__ import annotations

from starlette._body_limit import run_with_body_limit, validate_max_body_size
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """
    Limits the total size of HTTP request bodies, responding with a 413 to requests that exceed it.

    The limit is a cap: limits set further in, on a Starlette application, router, mount or route,
    can't raise it.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None) -> None:
        self.app = app
        self.max_body_size = validate_max_body_size(max_body_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(self.app, scope, receive, send, self.max_body_size, fail_fast=True, hard=True)


class _ApplicationBodyLimitMiddleware(RequestBodyLimitMiddleware):
    """
    Outermost layer of a `Starlette` application. It always counts the received bytes, and defers
    the `Content-Length` check until routing has settled which limit applies.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(self.app, scope, receive, send, self.max_body_size, fail_fast=False, force=True)
