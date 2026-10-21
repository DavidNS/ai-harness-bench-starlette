from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"
_BODY = b"Content Too Large"


class _BodyTooLarge(BaseException):
    """Raised inside the application to abort it once the limit is crossed.

    It derives from `BaseException` so that application code and the error handling middleware,
    which catch `Exception`, don't turn it into a second response.
    """


class _BodyLimitState:
    """Per request state, shared by every nested limit so an inner limit can override an outer one."""

    def __init__(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        self.limit = limit
        self.raw_receive = receive
        self.raw_send = send
        self.received = 0
        self.exceeded = False  # the limit was crossed, the body stream is stopped
        self.rejected = False  # we sent the 413 response
        self.started = False  # the application started its own response
        self.declared = _declared_length(scope)

    def declared_too_large(self) -> bool:
        return self.declared is not None and self.declared > self.limit

    async def reject(self) -> None:
        self.exceeded = True
        if self.started or self.rejected:
            return
        self.rejected = True
        await self.raw_send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(_BODY)).encode("latin-1")),
                ],
            }
        )
        await self.raw_send({"type": "http.response.body", "body": _BODY})

    async def _exceed(self) -> Message:
        was_rejected = self.rejected
        await self.reject()
        if self.rejected and not was_rejected:
            raise _BodyTooLarge()
        return {"type": "http.disconnect"}

    async def receive(self) -> Message:
        if self.exceeded:
            return {"type": "http.disconnect"}
        if self.declared_too_large():
            return await self._exceed()
        message = await self.raw_receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.received > self.limit:
                return await self._exceed()
        return message

    async def send(self, message: Message) -> None:
        if self.rejected:
            return
        if message["type"] == "http.response.start" and not self.started:
            if self.declared_too_large():
                await self.reject()
                return
            self.started = True
        await self.raw_send(message)


def _is_consequence(exc: BaseException) -> bool:
    sub_exceptions = getattr(exc, "exceptions", None)
    if sub_exceptions is not None:
        return all(_is_consequence(sub) for sub in sub_exceptions)
    return isinstance(exc, (Exception, _BodyTooLarge))


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                length = int(value)
            except ValueError:
                return None
            return length if length >= 0 else None
    return None


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    check_declared_length: bool = True,
) -> None:
    """Run `app`, enforcing `max_body_size` on HTTP requests.

    If a limit is already active for this request, `max_body_size` replaces it. With
    `check_declared_length`, an oversized `Content-Length` is rejected before `app` runs;
    callers that know a deeper layer may still change the limit pass `False` and the check
    is made by the router, or at the latest when the body is read or the response starts.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is not None:
        # Nested: the outermost layer owns the wrappers and error handling.
        if max_body_size is not None:
            state.limit = max_body_size
        if check_declared_length and state.declared_too_large():
            await state.reject()
            return
        await app(scope, receive, send)
        return

    if max_body_size is None:
        await app(scope, receive, send)
        return

    state = _BodyLimitState(scope, receive, send, max_body_size)
    scope[_SCOPE_KEY] = state
    try:
        if check_declared_length and state.declared_too_large():
            await state.reject()
            return
        await app(scope, state.receive, state.send)
        if state.declared_too_large() and not state.started:
            await state.reject()
    except BaseException as exc:
        # Once the 413 is sent, the failure of the aborted application (possibly wrapped in an
        # exception group by a middleware) is only a consequence of it.
        if not (state.rejected and _is_consequence(exc)):
            raise
    finally:
        scope.pop(_SCOPE_KEY, None)


async def reject_if_declared_too_large(scope: Scope) -> bool:
    """Send the 413 if the declared `Content-Length` exceeds the active limit. Returns whether it did."""
    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is not None and state.declared_too_large():
        await state.reject()
        return True
    return False


class RequestBodyLimitMiddleware:
    """Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413 response."""

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(self.app, scope, receive, send, self.max_body_size)
