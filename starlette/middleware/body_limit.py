from __future__ import annotations

from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SCOPE_KEY = "starlette.body_limit"


class _BodyLimitExceeded(BaseException):
    """
    Raised inside the application when the request body is over the limit.

    This derives from `BaseException` so that `except Exception` blocks and the
    exception handlers of an application don't turn it into some other response.
    """


def _is_limit_fallout(exc: BaseException) -> bool:
    """The limit error, or what BaseHTTPMiddleware turns it into when the app never answered."""
    return isinstance(exc, _BodyLimitExceeded) or (
        isinstance(exc, RuntimeError) and str(exc) == "No response returned."
    )


def _without_limit_error(exc: BaseException) -> BaseException | None:
    """What is left of `exc` once the limit error is taken out, `None` if nothing is."""
    split = getattr(exc, "split", None)  # (Base)ExceptionGroup
    if split is not None:
        _, rest = split(_is_limit_fallout)
        return rest  # type: ignore[no-any-return]
    return None if _is_limit_fallout(exc) else exc


def validate_max_body_size(max_body_size: int | None) -> None:
    if max_body_size is None:
        return
    if not isinstance(max_body_size, int) or isinstance(max_body_size, bool):
        raise TypeError("max_body_size must be an integer or None.")
    if max_body_size < 0:
        raise ValueError("max_body_size must be a non-negative integer or None.")


def defers_body_limit(app: Any) -> bool:
    """Whether `app`, or an app it wraps, is a router that settles the body limit itself once it has routed."""
    for _ in range(16):  # pragma: no branch
        if getattr(app, "_defers_body_limit", False):
            return True
        app = getattr(app, "app", None)
        if app is None:
            return False
    return False  # pragma: no cover


def inner_limit_ceiling(app: Any, _depth: int = 0) -> int:
    """The highest limit that routing below `app` could still set (0 if it sets none)."""
    best = 0
    seen = 0
    while app is not None and seen < 16:
        seen += 1
        limit = getattr(app, "max_body_size", None)
        if isinstance(limit, int):
            best = max(best, limit)
        router = getattr(app, "router", None)
        for route in getattr(router if router is not None else app, "routes", None) or []:
            if _depth < 16:
                limit = getattr(route, "max_body_size", None)
                if isinstance(limit, int):
                    best = max(best, limit)
                best = max(best, inner_limit_ceiling(getattr(route, "app", None), _depth + 1))
        app = getattr(app, "app", None)
    return best


_HUGE = 10**30


def _declared_length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length")
    if value is not None and value.isascii() and value.isdigit():
        digits = value.lstrip("0") or "0"
        # Avoid int() on absurdly long strings, any such length is over every limit.
        return int(digits) if len(digits) <= 30 else _HUGE
    return None


class _BodyLimitState:
    def __init__(self, scope: Scope, send: Send, base: int | None) -> None:
        self.scope = scope
        self.send = send
        # The limit that applies to what has been entered so far.
        self.limit = base
        # The limit of the application (or of the outermost router): what a request that matches
        # nothing inside a mount or router is checked against. `None` when there is none.
        self.base = base
        self.received = 0
        self.content_length = _declared_length(scope)
        self.exceeded = False
        self.app_started = False
        self.sent_413 = False

    def _fail(self) -> None:
        self.exceeded = True
        raise _BodyLimitExceeded()

    def override(self, limit: int | None) -> None:
        """A mount, router or route that was entered sets its own limit for what is below it."""
        if limit is not None:
            self.limit = limit
        self._check_received()

    def commit(self) -> None:
        """Routing has matched something that handles the request: the limit in effect is final."""
        self._check_declared()

    def commit_with_ceiling(self, ceiling: int) -> None:
        """Hand-off to something that may still raise the limit: reject only what no limit below could allow."""
        self._check_received()
        if self.limit is not None and self.content_length is not None:
            if self.content_length > max(self.limit, ceiling):
                self._fail()

    def settle_unmatched(self) -> None:
        """Nothing matched inside the mount or router that was entered: the application's limit applies."""
        self.limit = self.base
        self._check_declared()

    def _check_received(self) -> None:
        if self.exceeded or (self.limit is not None and self.received > self.limit):
            self._fail()

    def _check_declared(self) -> None:
        self._check_received()
        if self.limit is not None and self.content_length is not None and self.content_length > self.limit:
            self._fail()

    def count(self, size: int) -> None:
        self.received += size
        self._check_received()

    async def send_413(self) -> None:
        if not self.sent_413:
            self.sent_413 = True
            response = PlainTextResponse("Content Too Large", status_code=413)
            await response(self.scope, _no_receive, self.send)


async def _no_receive() -> Message:  # pragma: no cover
    raise RuntimeError("Receive channel has not been made available")


def settle_body_limit(scope: Scope) -> None:
    """Called by routing when nothing matched: the limit that applies is the one already in effect."""
    state: _BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is not None and scope["type"] == "http":
        state.settle_unmatched()


def commit_body_limit(scope: Scope) -> None:
    """Called by routing for a route of unknown kind that is about to handle the request."""
    state: _BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is not None and scope["type"] == "http":  # pragma: no cover
        state.commit()


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    commit: bool = False,
    ceiling: int | None = None,
    inner: bool = False,
    standalone: bool = False,
) -> None:
    """
    Run `app` with a request body limit.

    If a limit is already active for this request, the new one replaces it for what runs below.
    Otherwise this call becomes the outermost owner of the limit and answers with the 413.
    `commit` says that routing has decided and the declared `Content-Length` is checked now, `inner`
    that this is a mount or router inside an application, whose own limit is not the application's.
    A `standalone` limit is private and never changed by anything below it.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimitState | None = None if standalone else scope.get(SCOPE_KEY)
    if state is not None:
        state.override(max_body_size)
        if commit:
            state.commit()
        elif ceiling is not None:
            state.commit_with_ceiling(ceiling)
        await app(scope, receive, send)
        return

    if max_body_size is None:
        await app(scope, receive, send)
        return

    owner = state = _BodyLimitState(scope, send, None if inner else max_body_size)
    if inner:
        state.override(max_body_size)

    async def limited_receive() -> Message:
        owner.count(0)
        message = await receive()
        if message["type"] == "http.request":
            owner.count(len(message.get("body", b"")))
        return message

    async def limited_send(message: Message) -> None:
        if owner.exceeded and not owner.app_started:
            # Whatever the application tries to answer, the request was too large.
            await owner.send_413()
            return
        if message["type"] == "http.response.start":
            owner.app_started = True
        await send(message)

    if not standalone:
        scope[SCOPE_KEY] = state
    try:
        if commit or standalone:
            state.commit()
        elif ceiling is not None:
            state.commit_with_ceiling(ceiling)
        await app(scope, limited_receive, limited_send)
    except BaseException as exc:
        rest = _without_limit_error(exc) if state.exceeded else exc
        if rest is exc:
            raise
        if rest is not None:
            raise rest
    finally:
        if not standalone:
            scope.pop(SCOPE_KEY, None)
    if state.exceeded and not state.app_started:
        await state.send_413()


class RequestBodyLimitMiddleware:
    """
    Rejects requests whose body is larger than `max_body_size` bytes with a 413 response.

    The limit counts the body bytes actually received, the `Content-Length` header is
    only used to reject early.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(self.app, scope, receive, send, self.max_body_size, standalone=True)
