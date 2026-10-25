from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Scope key under which the `Starlette`, `Router`, `Mount` and `Route` limits share one limiter per request.
_SCOPE_KEY = "starlette.body_limiter"


class _BodyLimiter:
    """Per-request state: counts the body bytes received and rejects the request once over the limit."""

    def __init__(self, scope: Scope, receive: Receive, send: Send, limit: int | None) -> None:
        self.scope = scope
        self.limit = limit
        self._receive = receive
        self._send = send
        self.received = 0
        self.exceeded = False
        self.rejected = False
        self.response_started = False

    def declared_too_large(self) -> bool:
        if self.limit is None:
            return False
        for name, value in self.scope.get("headers", []):
            if name.lower() == b"content-length" and value.isascii() and value.strip().isdigit():
                if int(value) > self.limit:
                    return True
        return False

    async def reject(self) -> None:
        """Send the 413 response, unless the application has already started its own response."""
        if self.response_started or self.rejected:
            return
        self.rejected = True
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(self.scope, self._receive, self._send)

    async def receive(self) -> Message:
        if self.exceeded:
            return {"type": "http.disconnect"}
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.limit is not None and self.received > self.limit:
                self.exceeded = True
                await self.reject()
                return {"type": "http.disconnect"}
        return message

    async def send(self, message: Message) -> None:
        if self.rejected:
            return  # The 413 is the response; drop whatever the application still sends.
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)

    async def run(self, app: ASGIApp) -> None:
        try:
            await app(self.scope, self.receive, self.send)
        except Exception:
            # The application only fails here because we cut its body stream off.
            if not self.exceeded:
                raise


async def reject_if_declared_too_large(scope: Scope) -> bool:
    """Reject the request with a 413 if its Content-Length exceeds the limit currently in force."""
    limiter: _BodyLimiter | None = scope.get(_SCOPE_KEY)
    if limiter is not None and limiter.declared_too_large():
        await limiter.reject()
        return True
    return False


async def call_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    limit: int | None,
    *,
    settled: bool,
) -> None:
    """
    Call `app` with `limit` in force (`None` keeps the limit of the enclosing level, if any).

    If `settled` is true nothing below `app` can change the limit any more, so a declared
    `Content-Length` over the limit is rejected before `app` is called.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    limiter: _BodyLimiter | None = scope.get(_SCOPE_KEY)
    if limiter is not None:
        if limit is not None:
            limiter.limit = limit
        if settled and await reject_if_declared_too_large(scope):
            return
        await app(scope, receive, send)
        return

    if limit is None:
        await app(scope, receive, send)
        return

    limiter = _BodyLimiter(scope, receive, send, limit)
    scope[_SCOPE_KEY] = limiter
    if settled and await reject_if_declared_too_large(scope):
        return
    await limiter.run(app)


class RequestBodyLimitMiddleware:
    """
    Responds with `413 Content Too Large` to requests whose body exceeds `max_body_size` bytes.

    A declared `Content-Length` over the limit is rejected before the application is called;
    otherwise the body bytes received are counted and the body stream is stopped (the application
    sees `http.disconnect`) as soon as the limit is crossed.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limiter = _BodyLimiter(scope, receive, send, self.max_body_size)
        if limiter.declared_too_large():
            await limiter.reject()
            return
        await limiter.run(self.app)
