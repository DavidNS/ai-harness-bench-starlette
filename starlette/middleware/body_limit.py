from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.requests import ClientDisconnect
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATE_KEY = "starlette.body_limit"

_REJECTION_BODY = "Content Too Large"


class _BodyTooLarge(BaseException):
    """
    Raised from `receive()`/`send()` once the body crossed the limit, to stop the application.

    It is a `BaseException` so that error middleware and handlers of the application do not
    mistake an ordinary client error for an internal one.
    """


def validate_max_body_size(max_body_size: int | None) -> None:
    if max_body_size is None:
        return
    if isinstance(max_body_size, bool) or not isinstance(max_body_size, int):
        raise TypeError("max_body_size must be an int or None.")
    if max_body_size < 0:
        raise ValueError("max_body_size must not be negative.")


class _BodyLimitState:
    """Per-request counter shared by every layer that takes part in limiting a request."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.rejected = False
        self.response_started = False

    def declared_too_large(self, scope: Scope) -> bool:
        if self.received > self.limit:
            return True
        for name, value in scope.get("headers", ()):
            if name.lower() == b"content-length":
                digits = value.strip()
                if digits.isdigit():
                    # Compare as text-length first: huge digit strings must not reach int().
                    digits = digits.lstrip(b"0")
                    if len(digits) > 30 or (digits and int(digits) > self.limit):
                        return True
        return False


async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse(_REJECTION_BODY, status_code=413)(scope, receive, send)


def _caused_by_overrun(exc: BaseException) -> bool:
    # Besides the private stop signal, an application that was cut off from its body can fail with a
    # disconnect, or (behind `BaseHTTPMiddleware`, whose inner task was stopped) with "No response returned.".
    return isinstance(exc, (_BodyTooLarge, ClientDisconnect)) or (
        isinstance(exc, RuntimeError) and str(exc) == "No response returned."
    )


def _without_overrun_errors(exc: BaseException) -> BaseException | None:
    if hasattr(exc, "split") and hasattr(exc, "exceptions"):  # exception group
        _, rest = exc.split(_caused_by_overrun)
        return rest  # type: ignore[no-any-return]
    return None if _caused_by_overrun(exc) else exc


async def _run_owned(
    state: _BodyLimitState,
    scope: Scope,
    receive: Receive,
    send: Send,
    call: Callable[[Receive, Send], Awaitable[None]],
) -> None:
    """Run `call` with a receive channel that enforces `state.limit`, answering an overrun with a 413."""

    async def limited_receive() -> Message:
        if state.exceeded:
            return {"type": "http.disconnect"}  # pragma: no cover
        message = await receive()
        if message["type"] == "http.request":  # pragma: no branch
            state.received += len(message.get("body", b""))
            if state.received > state.limit:
                state.exceeded = True
                if state.response_started:
                    # Too late for a 413: stop the body stream, the application finishes its response.
                    return {"type": "http.disconnect"}
                state.response_started = True
                state.rejected = True
                await _reject(scope, receive, send)
                raise _BodyTooLarge()
        return message

    async def limited_send(message: Message) -> None:
        if state.rejected:
            raise _BodyTooLarge()
        if message["type"] == "http.response.start":
            state.response_started = True
        await send(message)

    try:
        await call(limited_receive, limited_send)
    except BaseException as exc:
        remaining = _without_overrun_errors(exc) if state.exceeded else exc
        if remaining is None:
            return
        if remaining is exc:
            raise
        raise remaining from exc.__cause__


async def run_with_body_limit(
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    call: Callable[[Receive, Send], Awaitable[None]],
) -> None:
    """
    Apply `max_body_size` for the part of the application below this call.

    The first layer that sets a limit owns the request: it counts the body and produces the 413.
    Layers further down only replace the limit that applies.
    """
    if scope["type"] != "http" or max_body_size is None:
        await call(receive, send)
        return
    state: _BodyLimitState | None = scope.get(STATE_KEY)
    if state is not None:
        state.limit = max_body_size
        await call(receive, send)
        return
    state = scope[STATE_KEY] = _BodyLimitState(max_body_size)
    await _run_owned(state, scope, receive, send, call)


async def reject_if_over_body_limit(scope: Scope, receive: Receive, send: Send) -> bool:
    """Reject the request if its declared length (or what was already read) is over the limit that applies."""
    state: _BodyLimitState | None = scope.get(STATE_KEY)
    if state is None or scope["type"] != "http" or not state.declared_too_large(scope):
        return False
    await _reject(scope, receive, send)
    return True


class _ApplicationBodyLimit:
    """Application level limit of `Starlette`; the limit can still be changed by routes below."""

    def __init__(self, app: ASGIApp, max_body_size: int | None) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(scope, receive, send, self.max_body_size, lambda r, s: self.app(scope, r, s))


class RequestBodyLimitMiddleware:
    """
    Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413 response.

    The limit counts the body bytes received from the server. `Content-Length` is only used to
    reject early.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return
        state = _BodyLimitState(self.max_body_size)
        if state.declared_too_large(scope):
            await _reject(scope, receive, send)
            return
        await _run_owned(state, scope, receive, send, lambda r, s: self.app(scope, r, s))
