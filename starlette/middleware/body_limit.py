from __future__ import annotations

from types import TracebackType

import anyio

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Key under which Starlette's routing shares the limit in force for a request,
# so that an inner route, mount or router can raise or lower an outer limit.
SCOPE_KEY = "starlette.body_limit"

_BODY = b"Content Too Large"


async def _send_too_large(send: Send) -> None:
    # Fresh messages each time, as middleware may edit them in place. Shielded so that the
    # cancellation of a task of the application cannot leave the response half sent.
    start: Message = {
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-length", str(len(_BODY)).encode("latin-1")),
            (b"content-type", b"text/plain; charset=utf-8"),
        ],
    }
    with anyio.CancelScope(shield=True):
        await send(start)
        await send({"type": "http.response.body", "body": _BODY})


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name.lower() == b"content-length":
            text = value.decode("latin-1").strip()
            if not (text.isascii() and text.isdigit()):
                return None
            digits = text.lstrip("0") or "0"
            # Python refuses to convert very long digit strings, those are over any limit anyway.
            return int(digits) if len(digits) <= 1000 else 10**1000
    return None


def defers_body_limit(app: object) -> bool:
    """
    Whether `app` is a router (or Starlette app) that settles the limit itself once it has routed,
    possibly behind Starlette's own middleware, which always forwards to the app it wraps.

    Any other app is opaque: nothing says that a router it refers to is the one handling the request,
    so the caller has to apply the limit it knows before the app runs.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    while True:
        if getattr(app, "_checks_body_limit", False):
            return True
        module = type(app).__module__
        if not (module.startswith("starlette.middleware.") or isinstance(app, BaseHTTPMiddleware)):
            return False
        inner = getattr(app, "app", None)
        if inner is None:
            return False
        app = inner


class _BodyLimit:
    """Counts the body bytes received from the server and enforces a limit on them."""

    def __init__(self, receive: Receive, send: Send, limit: int) -> None:
        self._receive = receive
        self._send = send
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.rejected = False  # Whether we sent the 413 response from `receive`.
        self.response_started = False

    async def receive(self) -> Message:
        if self.exceeded:
            return {"type": "http.disconnect"}
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.received > self.limit:
                self.exceeded = True
                if not self.response_started:
                    # The application may be anywhere in its stack, so respond right away
                    # and drop whatever it sends afterwards.
                    self.rejected = True
                    self.response_started = True
                    await _send_too_large(self._send)
                return {"type": "http.disconnect"}
        return message

    async def send(self, message: Message) -> None:
        if self.rejected:
            return
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)

    async def reject_if_too_large(self, scope: Scope, send: Send) -> bool:
        """Respond with a 413 through `send` if the request is already known to be over the limit."""
        declared = _declared_length(scope)
        if self.received <= self.limit and (declared is None or declared <= self.limit):
            return False
        self.exceeded = True
        if not self.response_started:
            await _send_too_large(send)
        return True


async def reject_if_too_large(scope: Scope, send: Send) -> bool:
    """Fail fast on a declared `Content-Length` over the limit in force. Returns `True` if rejected."""
    state: _BodyLimit | None = scope.get(SCOPE_KEY)
    return state is not None and await state.reject_if_too_large(scope, send)


class limit_scope:
    """
    Sets `max_body_size` as the limit in force for the request, replacing an outer one.
    Where no limit is in force yet, it starts counting the body from here.
    """

    def __init__(self, scope: Scope, receive: Receive, send: Send, max_body_size: int | None) -> None:
        self.scope = scope
        self.receive = receive
        self.send = send
        self.max_body_size = max_body_size
        self.created: _BodyLimit | None = None

    async def __aenter__(self) -> tuple[Receive, Send]:
        scope, receive, send, limit = self.scope, self.receive, self.send, self.max_body_size
        if scope["type"] != "http" or limit is None:
            return receive, send
        state: _BodyLimit | None = scope.get(SCOPE_KEY)
        if state is not None:
            state.limit = limit
            return receive, send
        self.created = scope[SCOPE_KEY] = _BodyLimit(receive, send, limit)
        return self.created.receive, self.created.send

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool:
        if self.created is None:
            return False
        del self.scope[SCOPE_KEY]
        # The client got its 413 and the application was told that the body stream ended,
        # whatever it raised as a consequence is of no interest.
        return self.created.exceeded and isinstance(exc, Exception)


class RequestBodyLimitMiddleware:
    """
    Rejects HTTP requests whose body is larger than `max_body_size` bytes with a 413 response.

    A `Content-Length` over the limit is rejected before the application is called,
    anything else is stopped as soon as the bytes received exceed the limit.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None) -> None:
        assert max_body_size is None or max_body_size >= 0, "'max_body_size' must not be negative"
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return

        # This limit is private to the middleware, the application below cannot change it.
        state = _BodyLimit(receive, send, self.max_body_size)
        if await state.reject_if_too_large(scope, send):
            return
        try:
            await self.app(scope, state.receive, state.send)
        except Exception:
            if not state.exceeded:
                raise


__all__ = ["RequestBodyLimitMiddleware"]
