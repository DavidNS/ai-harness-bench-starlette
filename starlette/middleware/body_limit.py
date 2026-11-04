from __future__ import annotations

import sys

import anyio

from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if sys.version_info < (3, 11):  # pragma: no cover
    from exceptiongroup import BaseExceptionGroup

_SCOPE_KEY = "starlette.body_limit"
_MAX_DIGITS = 30


class _BodyTooLarge(BaseException):
    """
    Raised to cut off an application whose request exceeds its body limit.

    This is a `BaseException`, so that `except Exception` clauses, error handlers and
    `ServerErrorMiddleware` leave it alone: an oversized request is not a server error. The
    limiter that owns the limit turns it into a `413` response.
    """

    def __init__(self, state: _State) -> None:
        self.state = state


class _State:
    def __init__(self, max_body_size: int) -> None:
        self.max_body_size = max_body_size
        self.received = 0
        self.exceeded = False
        self.response_started = False
        # A `Content-Length` check waiting for the routing below to settle the limit.
        self.pending_check = False


def is_body_limit_error(exc: BaseException) -> bool:
    """Whether `exc` is the body limit signal, possibly inside exception groups."""
    if isinstance(exc, BaseExceptionGroup):
        return any(is_body_limit_error(inner) for inner in exc.exceptions)
    return isinstance(exc, _BodyTooLarge)


def _only_limit_signal(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return all(_only_limit_signal(inner) for inner in exc.exceptions)
    # `ClientDisconnect` is what the body stream turns the disconnect we send into.
    return isinstance(exc, (_BodyTooLarge, ClientDisconnect))


def _can_be_replaced_by_413(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return all(_can_be_replaced_by_413(inner) for inner in exc.exceptions)
    return isinstance(exc, (Exception, _BodyTooLarge))


def _declared_length(scope: Scope) -> int | None:
    value: str | None = Headers(scope=scope).get("content-length")
    if value is None:
        return None
    value = value.strip()
    if not (value.isascii() and value.isdigit()):
        return None
    digits = value.lstrip("0") or "0"
    if len(digits) > _MAX_DIGITS:
        # Over any limit, and too long to convert safely.
        return int("1" + "0" * _MAX_DIGITS)
    return int(digits)


def _check_declared_length(scope: Scope, state: _State) -> None:
    state.pending_check = False
    length = _declared_length(scope)
    if length is not None and length > state.max_body_size:
        state.exceeded = True
        raise _BodyTooLarge(state)


def defer_content_length_check(scope: Scope) -> None:
    """
    Check the declared `Content-Length` once the routing below has had its say.

    The check is made by whatever settles the limit first (a matched route, a missing route
    or method), and otherwise when the application below first receives, responds or returns.
    """
    state: _State | None = scope.get(_SCOPE_KEY)
    if state is not None:
        state.pending_check = True


def check_content_length(scope: Scope) -> None:
    """Reject a request whose declared `Content-Length` is over the limit that applies."""
    state: _State | None = scope.get(_SCOPE_KEY)
    if state is not None:
        _check_declared_length(scope, state)


async def _run(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    state: _State,
    *,
    check: bool,
    register: bool,
) -> None:
    rejected = False

    async def reject() -> None:
        nonlocal rejected
        rejected = True
        state.response_started = True
        await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)

    def settle() -> None:
        if state.pending_check:
            _check_declared_length(scope, state)

    async def limited_receive() -> Message:
        settle()
        if not state.exceeded:
            message = await receive()
            state.received += len(message.get("body", b""))
            if state.received <= state.max_body_size:
                return message
            if not state.response_started:
                # Let a response that is ready to start do so before the limit is judged,
                # e.g. while a disconnect listener drains the body. Only done once the
                # limit is crossed, so polling for a disconnect is not slowed down.
                # Shielded, so that polling for a disconnect, which runs under a cancelled
                # scope, still settles the limit instead of dropping the message.
                with anyio.CancelScope(shield=True):
                    await anyio.sleep(0)
            state.exceeded = True
        if state.response_started:
            # Too late for a 413, just stop the body.
            return {"type": "http.disconnect"}
        raise _BodyTooLarge(state)

    async def limited_send(message: Message) -> None:
        if rejected:
            return
        settle()
        if state.exceeded and not state.response_started:
            # The application carries on after it was cut off, e.g. with its own error
            # response. The client gets the 413 instead.
            await reject()
            return
        if message["type"] == "http.response.start":
            state.response_started = True
        await send(message)

    if register:
        scope[_SCOPE_KEY] = state
    try:
        if check:
            _check_declared_length(scope, state)
        await app(scope, limited_receive, limited_send)
        settle()
    except BaseException as exc:
        # Once the response has started there is no 413 to send, and only the limit's own
        # signal is dropped: any other error is the application's and must be seen.
        replaceable = _can_be_replaced_by_413(exc) if not state.response_started else _only_limit_signal(exc)
        if not (state.exceeded and replaceable):
            raise
    finally:
        if register:
            scope.pop(_SCOPE_KEY, None)
    if state.exceeded and not state.response_started:
        await reject()


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    check: bool,
    defer: bool = False,
) -> None:
    """
    Run `app` with the given body limit, for the routing tree of a Starlette application.

    If a limit is already in force for this request, `max_body_size` replaces it (when not
    `None`). Otherwise a new limit is started, and this call is the one that sends the `413`.
    `check` says whether the `Content-Length` header is checked against the limit now, or
    left to whatever sits below, which still has a chance to change the limit. With `defer`
    the check is made once that has happened, see `defer_content_length_check`.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _State | None = scope.get(_SCOPE_KEY)
    if state is not None:
        if max_body_size is not None:
            state.max_body_size = max_body_size
            if state.received > max_body_size:
                state.exceeded = True
                raise _BodyTooLarge(state)
        if check:
            _check_declared_length(scope, state)
        state.pending_check = state.pending_check or defer
        await app(scope, receive, send)
    elif max_body_size is None:
        await app(scope, receive, send)
    else:
        new_state = _State(max_body_size)
        new_state.pending_check = defer
        await _run(app, scope, receive, send, new_state, check=check, register=True)


class RequestBodyLimitMiddleware:
    """
    Limits the size of HTTP request bodies, responding with `413` when exceeded.

    The limit counts the body bytes received from the server. A declared `Content-Length`
    over the limit is rejected before the application is called. Nothing below the middleware
    can change its limit: limits set by routes, mounts or routers further in apply on top.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await _run(self.app, scope, receive, send, _State(self.max_body_size), check=True, register=False)
