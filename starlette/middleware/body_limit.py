from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from starlette.requests import ClientDisconnect
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"

# A declared length with more digits than this is far over any limit; it isn't converted to an int.
_MAX_CONTENT_LENGTH_DIGITS = 32


class RequestBodyTooLarge(Exception):
    """
    Raised from `receive()` once the request body exceeds the limit.

    It travels up to the layer that owns the limit, which sends the `413` response.
    """


def _parse_content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            value = bytes(value).strip()
            if not (value.isascii() and value.isdigit()):
                return None
            if len(value) > _MAX_CONTENT_LENGTH_DIGITS:
                return int("9" * _MAX_CONTENT_LENGTH_DIGITS)
            return int(value)
    return None


class _BodyLimitState:
    """
    Per-request state shared by every layer taking part in limiting the body.

    `caps` are the limits of standalone middleware: nothing below them can change them.
    `soft` is the limit set by the Starlette application, router, mount or route in force,
    which an inner one replaces. `base` is the application's own limit, which applies to a
    request that matches no route or no method.
    """

    def __init__(self, receive: Receive, scope: Scope, base: int | None) -> None:
        self.raw_receive = receive
        self.base = base
        self.soft = base
        self.caps: list[int] = []
        self.received = 0
        self.pending = False  # the limit still has to be settled once routing has decided
        self.exceeded = False
        self.response_started = False
        self.content_length = _parse_content_length(scope)

    @property
    def limit(self) -> int | None:
        limits = self.caps if self.soft is None else [*self.caps, self.soft]
        return min(limits) if limits else None

    def check(self) -> None:
        """Fail fast on a declared length, or a body already received, over the limit in force."""
        limit = self.limit
        if limit is None:
            return
        if self.received > limit or (self.content_length is not None and self.content_length > limit):
            self.exceeded = True
            raise RequestBodyTooLarge()

    def settle(self) -> None:
        self.pending = False
        self.check()

    async def receive(self) -> Message:
        if self.pending:
            self.settle()
        if self.exceeded:
            if self.response_started:
                return {"type": "http.disconnect"}
            raise RequestBodyTooLarge()
        message = await self.raw_receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            limit = self.limit
            if limit is not None and self.received > limit:
                self.exceeded = True
                if self.response_started:
                    return {"type": "http.disconnect"}
                raise RequestBodyTooLarge()
        return message


def body_limit_exceeded(scope: Scope) -> bool:
    """Whether the request in `scope` has crossed its body limit, and a 413 is due."""
    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    return state is not None and state.exceeded


def settle_body_limit(scope: Scope, *, application: bool = False) -> None:
    """
    Called once routing has decided which limit applies to the request.

    With `application=True` nothing matched, so the application's own limit applies.
    """
    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is not None:
        if application:
            state.soft = state.base
        state.settle()


def _is_limit_error(exc: BaseException) -> bool:
    """Whether `exc` is what the limit itself causes in the application: possibly in an exception group."""
    if isinstance(exc, RequestBodyTooLarge | ClientDisconnect):
        return True
    nested: Sequence[BaseException] = getattr(exc, "exceptions", ())
    return bool(nested) and all(_is_limit_error(e) for e in nested)


async def limit_body(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    role: Literal["cap", "app", "inner"],
    settle: Literal["now", "lazy", "never"] = "never",
) -> None:
    """
    Run `app`, enforcing `max_body_size` on the request body.

    - `cap`: standalone middleware. Its limit holds whatever is set below it, and the
      declared length is checked on entry.
    - `app`: a Starlette application, whose limit is the one for unmatched requests.
    - `inner`: a router, mount or route. A non-`None` limit replaces the one in force.

    The layer that finds no state in the scope owns the limit and sends the `413`.
    `settle` says when the declared length is checked against the final limit: right now,
    on the first `receive()` or response start unless a router or route below settles it
    earlier (`lazy`, for an app that may route further out of sight), or never (something
    below settles it once routing has decided).
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is not None:
        if role == "cap":
            assert max_body_size is not None
            state.caps.append(max_body_size)
        elif max_body_size is not None:
            state.soft = max_body_size
            if role == "app":
                state.base = max_body_size
        try:
            if role == "cap":
                state.check()
            elif settle == "now":
                state.settle()
            elif settle == "lazy":
                state.pending = True
            await app(scope, receive, send)
        finally:
            if role == "cap":
                state.caps.remove(max_body_size)  # type: ignore[arg-type]
        return

    if max_body_size is None:
        await app(scope, receive, send)
        return

    if role == "cap":
        state = _BodyLimitState(receive, scope, None)
        state.caps.append(max_body_size)
    else:
        # Inside an application without a limit of its own, unmatched requests are unlimited.
        state = _BodyLimitState(receive, scope, max_body_size if role == "app" or "app" not in scope else None)
        state.soft = max_body_size
    scope[_SCOPE_KEY] = state
    owned = state

    async def send_wrapper(message: Message) -> None:
        if message["type"] == "http.response.start":
            if owned.pending:
                owned.settle()
            owned.response_started = True
        await send(message)

    try:
        if role == "cap" or settle == "now":
            state.settle()
        elif settle == "lazy":
            state.pending = True
        await app(scope, state.receive, send_wrapper)
    except Exception as exc:
        # Crossing the limit is what the request failed on, even if the application
        # swallowed that and failed differently. Once the response has started, only the
        # failures the limit itself causes are dropped.
        if not state.exceeded or (state.response_started and not _is_limit_error(exc)):
            raise
    finally:
        scope.pop(_SCOPE_KEY, None)

    if state.exceeded and not state.response_started:
        await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send_wrapper)


class RequestBodyLimitMiddleware:
    """
    ASGI middleware rejecting HTTP requests whose body is larger than `max_body_size`
    bytes with a `413 Content Too Large` response.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await limit_body(self.app, scope, receive, send, self.max_body_size, role="cap")
