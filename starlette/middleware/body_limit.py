from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.request_body_limit"


class RequestBodyTooLarge(Exception):
    """Raised from `receive()` when the request body exceeds the applicable limit."""


class _BodyLimit:
    """The per-request state shared by every layer that sets a limit."""

    def __init__(self, receive: Receive, max_body_size: int) -> None:
        self.max_body_size = max_body_size
        self.received = 0
        self.exceeded = False
        self.response_started = False
        self._receive = receive

    async def receive(self) -> Message:
        if self.exceeded:
            raise RequestBodyTooLarge()
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            self._verify()
        return message

    async def send(self, send: Send, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.response_started = True
        await send(message)

    def set_limit(self, max_body_size: int) -> None:
        self.max_body_size = max_body_size
        self._verify()

    def check_content_length(self, scope: Scope) -> None:
        value = Headers(scope=scope).get("content-length")
        if value is None:
            return
        try:
            declared = int(value)
        except ValueError:
            return
        if declared > self.max_body_size:
            self.exceeded = True
            raise RequestBodyTooLarge()

    def _verify(self) -> None:
        if self.received > self.max_body_size:
            self.exceeded = True
            raise RequestBodyTooLarge()


def settle_body_limit(scope: Scope) -> None:
    """
    Reject a request whose declared `Content-Length` exceeds the limit that applies,
    once routing has decided which limit that is. Does nothing if no limit is active.
    """
    if scope["type"] == "http":
        limit: _BodyLimit | None = scope.get(_SCOPE_KEY)
        if limit is not None:
            limit.check_content_length(scope)


async def call_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    settle: bool = True,
) -> None:
    """
    Call `app` with `max_body_size` applied.

    The outermost caller to set a limit owns the request: it counts the received bytes and
    sends the 413 response. Any layer below it only replaces the limit, which may raise or
    lower it. With `settle` the declared `Content-Length` is checked before `app` is called.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    limit: _BodyLimit | None = scope.get(_SCOPE_KEY)
    if limit is not None:
        if max_body_size is not None:
            limit.set_limit(max_body_size)
        if settle:
            limit.check_content_length(scope)
        await app(scope, receive, send)
        return

    if max_body_size is None:
        await app(scope, receive, send)
        return

    limit = _BodyLimit(receive, max_body_size)
    scope[_SCOPE_KEY] = limit

    async def limited_send(message: Message) -> None:
        await limit.send(send, message)

    try:
        if settle:
            limit.check_content_length(scope)
        await app(scope, limit.receive, limited_send)
    except RequestBodyTooLarge:
        pass

    if limit.exceeded and not limit.response_started:
        await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)


class RequestBodyLimitMiddleware:
    """
    Limits the size of the request body, responding with `413 Content Too Large` when exceeded.
    """

    _settle = True

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        assert max_body_size >= 0, "max_body_size must not be negative"
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await call_with_body_limit(self.app, scope, receive, send, self.max_body_size, settle=self._settle)


class _RoutedRequestBodyLimitMiddleware(RequestBodyLimitMiddleware):
    """The application-level limit: the router settles the `Content-Length` check."""

    _settle = False
