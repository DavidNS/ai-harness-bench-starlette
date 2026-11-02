from __future__ import annotations

from collections.abc import Sequence

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised inside the application when the request body crosses the limit."""

    def __init__(self, state: _BodyLimitState) -> None:
        super().__init__("Request body exceeds the maximum allowed size.")
        self.state = state


class _BodyLimitState:
    """Per-request bookkeeping shared by everything that applies a body limit."""

    def __init__(self, limit: int | None, scope: Scope, receive: Receive, send: Send) -> None:
        self.limit = limit
        self.base_limit = limit  # the outermost limit, which alone governs requests that match nothing
        self.received = 0
        self.exceeded = False
        self.started = False
        self.rejected = False
        self.scope = scope
        self._receive = receive
        self._send = send
        self.content_length = self._parse_content_length(scope)

    @staticmethod
    def _parse_content_length(scope: Scope) -> int | None:
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                value = value.strip()
                if not value.isdigit():
                    return None
                # Avoid int() on absurdly long digit strings, which raises ValueError.
                return int(value) if len(value) <= 30 else 10**30
        return None

    def check(self, *, base: bool = False) -> None:
        """Fail fast: raise if the declared length, or what was read so far, is over the limit."""
        limit = self.base_limit if base else self.limit
        if self.exceeded or (limit is not None and (self.received > limit or (self.content_length or 0) > limit)):
            self.exceeded = True
            raise RequestBodyTooLarge(self)

    def set_limit(self, limit: int | None) -> None:
        self.limit = limit

    async def receive(self) -> Message:
        if self.exceeded:
            return self._stop()
        message = await self._receive()
        if message["type"] == "http.request":  # pragma: no branch
            self.received += len(message.get("body", b""))
            if self.limit is not None and self.received > self.limit:
                self.exceeded = True
                return self._stop()
        return message

    def _stop(self) -> Message:
        if self.started:
            # Too late for a 413, so the body stream is simply cut off.
            return {"type": "http.disconnect"}
        raise RequestBodyTooLarge(self)

    async def send(self, message: Message) -> None:
        if self.rejected:
            return
        if message["type"] == "http.response.start" and not self.started:
            if self.exceeded:
                # The application got cut off, whatever it wanted to answer is moot.
                await self.send_rejection()
                return
            self.started = True
        await self._send(message)

    async def send_rejection(self) -> None:
        self.started = True
        self.rejected = True
        response = PlainTextResponse("Content Too Large", status_code=413)

        async def receive() -> Message:  # pragma: no cover
            raise RuntimeError("receive is not available")

        await response(self.scope, receive, self._send)

    def _is_ours(self, exc: BaseException) -> bool:
        """Whether `exc`, possibly wrapped in exception groups by task groups, is only our rejection."""
        if isinstance(exc, RequestBodyTooLarge):
            return exc.state is self
        inner: Sequence[BaseException] = getattr(exc, "exceptions", ())
        return bool(inner) and all(self._is_ours(e) for e in inner)

    async def run(self, app: ASGIApp, *, settle: bool) -> None:
        try:
            if settle:
                self.check()
            await app(self.scope, self.receive, self.send)
        except BaseException as exc:
            if not self._is_ours(exc):
                raise
        if self.exceeded and not self.started:
            await self.send_rejection()


async def apply_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    limit: int | None,
    *,
    settle: bool,
    root: bool = False,
) -> None:
    """Run `app` under the body limit of the surrounding Starlette tree.

    `limit` overrides the limit already in effect, `None` inherits it. With `settle`
    the declared Content-Length is checked against the limit that applies, because
    nothing below this point can change it any more.
    """
    state: _BodyLimitState | None = scope.get(SCOPE_KEY)
    if scope["type"] != "http" or (state is None and limit is None and not root):
        await app(scope, receive, send)
        return

    if state is not None:
        if limit is not None:
            state.set_limit(limit)
        if settle:
            state.check()
        await app(scope, receive, send)
        return

    state = _BodyLimitState(limit, scope, receive, send)
    scope[SCOPE_KEY] = state
    try:
        await state.run(app, settle=settle)
    finally:
        scope.pop(SCOPE_KEY, None)


def settle_body_limit(scope: Scope) -> None:
    """Fail fast for a request that matched no route or no method: only the outermost limit applies."""
    state: _BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is not None:
        state.set_limit(state.base_limit)
        state.check(base=True)


class _ScopeBodyLimitMiddleware:
    """Counts the body of the whole application; the Content-Length check waits for routing."""

    def __init__(self, app: ASGIApp, max_body_size: int | None) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await apply_body_limit(self.app, scope, receive, send, self.max_body_size, settle=False, root=True)


class RequestBodyLimitMiddleware:
    """Rejects requests whose body is larger than `max_body_size` bytes with a 413."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        assert max_body_size >= 0, "max_body_size must not be negative"
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Not stored in the scope: nothing below this middleware may change its limit.
        state = _BodyLimitState(self.max_body_size, scope, receive, send)
        await state.run(self.app, settle=True)
