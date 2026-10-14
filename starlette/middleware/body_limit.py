from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class _BodyTooLarge(Exception):
    pass


def _is_body_too_large(exc: BaseException) -> bool:
    # Task groups, as used by `BaseHTTPMiddleware`, wrap the error in an exception group.
    group = getattr(exc, "exceptions", None)
    if group is not None:
        return bool(group) and all(_is_body_too_large(e) for e in group)
    return isinstance(exc, _BodyTooLarge)


class _BodyLimitState:
    """Per-request state, shared by every body limit layer the request passes through."""

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.length_checked = False


class RequestBodyLimitMiddleware:
    """
    Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413 response.

    The limit counts the body bytes received from the ASGI server. A declared `Content-Length`
    over the limit is rejected before any of the body is read, but the header is never trusted:
    the received bytes are always counted.

    When the middleware is nested, the innermost `max_body_size` that is not `None` wins, so an
    inner layer can raise or lower the limit of an outer one.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        assert max_body_size is None or max_body_size >= 0, "'max_body_size' must not be negative."
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return

        state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
        if state is not None:
            # An outer layer already counts the body, only override its limit.
            state.limit = self.max_body_size
            state.length_checked = False
        else:
            state = scope[_SCOPE_KEY] = _BodyLimitState(self.max_body_size)
            receive = self._wrap_receive(scope, receive, state)

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if not _is_body_too_large(exc):
                raise
            if not response_started:
                response = PlainTextResponse("Content Too Large", status_code=413)
                await response(scope, receive, send_wrapper)

    @staticmethod
    def _wrap_receive(scope: Scope, receive: Receive, state: _BodyLimitState) -> Receive:
        async def receive_wrapper() -> Message:
            if state.exceeded:
                raise _BodyTooLarge()

            # Checked on the first read rather than on entry, so that a limit set by an
            # inner route, mount or router is the one applied.
            if not state.length_checked:
                state.length_checked = True
                content_length = Headers(scope=scope).get("content-length")
                if (
                    state.limit is not None
                    and content_length is not None
                    and content_length.isascii()
                    and content_length.isdecimal()
                    and int(content_length) > state.limit
                ):
                    state.exceeded = True
                    raise _BodyTooLarge()

            message = await receive()
            if message["type"] == "http.request":
                state.received += len(message.get("body", b""))
                if state.limit is not None and state.received > state.limit:
                    state.exceeded = True
                    raise _BodyTooLarge()
            return message

        return receive_wrapper
