You are reviewing one task of an approved plan, implemented by a different session whose
reasoning you have not seen. You can read the repository (the change is already applied in this
worktree) but not edit it or run commands. The harness has already verified mechanically that the
diff stays within the task's files, that the changed tests fail without the production change and
pass with it, and that the whole suite is green. Do not re-check those. Your job is judgement.

## Original request

Add configurable limits for the total HTTP request body size to Starlette, as upstream did in
encode/starlette PR #3431 (closes #2155).

Requirements:

1. New module `starlette/middleware/body_limit.py` with a pure ASGI middleware
   `RequestBodyLimitMiddleware(app, max_body_size: int)`. It wraps `receive` so that the body bytes
   actually received from the ASGI server are counted, including multipart file data and encoding
   overhead. When the total exceeds `max_body_size`, the request is answered with a plain-text
   `413 Content Too Large` response. `Content-Length` is used only as a fail-fast check: a request
   whose declared length already exceeds the limit is rejected before its body is read, but an
   absent or understated header never bypasses the limit. If the application has already started
   sending a response when the limit is crossed, the middleware must not send a second response;
   the body limit must still stop the receive stream by raising
   `starlette.exceptions.HTTPException(413)` from `receive`, so the standard exception handling turns
   it into the 413 response (or re-raises it when the response has already started). The module
   exports the public constant `MAX_BODY_SIZE_SCOPE_KEY = "starlette.max_body_size"`: the effective
   limit is stored in the ASGI scope under that key while a limited app runs and the previous value
   (or its absence) is restored afterwards.
2. A `max_body_size: int | None = None` keyword on `Starlette`, `Router`, `Mount` and `Route`.
   `None` keeps today's unlimited behaviour. The application-level limit is applied through the
   middleware stack before routing. A limit on an inner route, mount or router overrides the outer
   one after routing, so a route can accept a larger body than the rest of the application.
   Application middleware that reads the body is subject to the application-level limit. The
   fail-fast on `Content-Length` applies even when the endpoint never reads the body.
3. Multipart parsing (`starlette/formparsers.py`) must close any temporary files it created when
   a parse is interrupted for whatever reason, including a body-limit error raised mid-stream.
4. Export nothing new at the package root; the middleware is imported from
   `starlette.middleware.body_limit`.

Tests: add tests for the middleware (limit enforced with and without `Content-Length`, understated
`Content-Length`, exactly-at-limit requests allowed, multipart uploads, streaming bodies, the
no-double-response case) and for `max_body_size` on `Starlette`, `Router`, `Mount` and `Route`
including an inner limit raising and lowering the outer one, plus the multipart temp-file cleanup.
The existing suite must stay green. Do not change unrelated behaviour.

## Plan summary

T1 added the middleware and multipart cleanup, but the Starlette/Router/Mount/Route wiring is not in the worktree, despite the earlier note. T2 changes the middleware so the Content-Length fail-fast is deferred until the effective limit is final: inner limits can raise the limit, and endpoints that never read the body are still rejected. T3 adds the `max_body_size` wiring and tests/test_max_body_size.py.

## Task T4 (T3)

Add the max_body_size keyword to Starlette, Router, Mount and Route. Starlette inserts the middleware after ServerErrorMiddleware and before user middleware. Route, Mount and Router wrap their app outermost, only when the value is not None. Add tests/test_max_body_size.py for all four classes, including inner limits that raise and lower the outer one, app middleware that reads the body, and multipart temp-file cleanup under a limit.

Acceptance criteria:
- ACC17 [behavior]: Starlette, Router, Mount and Route accept max_body_size. None, the default, leaves the body unlimited, and an oversized body is rejected with 413 when a limit is set.
- ACC18 [behavior]: Application middleware that reads the body is subject to the Starlette(max_body_size=...) limit, and an oversized body gets 413.
- ACC19 [behavior]: A limit on an inner Route, Mount or Router overrides the outer limit. It can raise the limit so a larger body is accepted, or lower it so a body the outer limit allows is rejected.
- ACC20 [behavior]: Temp files created by multipart parsing are closed when parsing is interrupted, including by a body-limit error.
- ACC21 [other]: Tests exist in tests/middleware/test_body_limit.py and tests/test_max_body_size.py covering the listed scenarios, and the existing suite stays green.

Files declared for the task:
- starlette/applications.py (production)
- starlette/routing.py (production)
- tests/test_max_body_size.py (test)

Test files of this task that the change modified (existing tests, declared by the plan):
- (none)

Test results: selected tests green; red proven; suite green

## The diff

```diff
diff --git a/starlette/applications.py b/starlette/applications.py
index 3e1a781..6a25c31 100644
--- a/starlette/applications.py
+++ b/starlette/applications.py
@@ -5,6 +5,7 @@ from typing import Any, ParamSpec, TypeVar
 
 from starlette.datastructures import State, URLPath
 from starlette.middleware import Middleware, _MiddlewareFactory
+from starlette.middleware.body_limit import RequestBodyLimitMiddleware
 from starlette.middleware.errors import ServerErrorMiddleware
 from starlette.middleware.exceptions import ExceptionMiddleware
 from starlette.requests import Request
@@ -26,6 +27,7 @@ class Starlette:
         middleware: Sequence[Middleware] | None = None,
         exception_handlers: Mapping[Any, ExceptionHandler] | None = None,
         lifespan: Lifespan[AppType] | None = None,
+        max_body_size: int | None = None,
     ) -> None:
         """Initializes the application.
 
@@ -46,8 +48,11 @@ class Starlette:
             lifespan: A lifespan context function, which can be used to perform
                 startup and shutdown tasks. This is a newer style that replaces the
                 `on_startup` and `on_shutdown` handlers. Use one or the other, not both.
+            max_body_size: Maximum size in bytes of the request body. Larger requests
+                are answered with a 413 response. `None` means no limit.
         """
         self.debug = debug
+        self.max_body_size = max_body_size
         self.state = State()
         self.router = Router(routes, lifespan=lifespan)
         self.exception_handlers = {} if exception_handlers is None else dict(exception_handlers)
@@ -65,8 +70,12 @@ class Starlette:
             else:
                 exception_handlers[key] = value
 
+        body_limit_middleware: list[Middleware] = []
+        if self.max_body_size is not None:
+            body_limit_middleware.append(Middleware(RequestBodyLimitMiddleware, max_body_size=self.max_body_size))
         middleware = (
             [Middleware(ServerErrorMiddleware, handler=error_handler, debug=debug)]
+            + body_limit_middleware
             + self.user_middleware
             + [Middleware(ExceptionMiddleware, handlers=exception_handlers, debug=debug)]
         )
diff --git a/starlette/routing.py b/starlette/routing.py
index a035d18..c4b9563 100644
--- a/starlette/routing.py
+++ b/starlette/routing.py
@@ -20,6 +20,7 @@ from starlette.convertors import CONVERTOR_TYPES, Convertor
 from starlette.datastructures import URL, Headers, URLPath
 from starlette.exceptions import HTTPException, StarletteDeprecationWarning
 from starlette.middleware import Middleware
+from starlette.middleware.body_limit import RequestBodyLimitMiddleware
 from starlette.requests import Request
 from starlette.responses import PlainTextResponse, RedirectResponse, Response
 from starlette.types import ASGIApp, Lifespan, Receive, Scope, Send
@@ -203,6 +204,7 @@ class Route(BaseRoute):
         name: str | None = None,
         include_in_schema: bool = True,
         middleware: Sequence[Middleware] | None = None,
+        max_body_size: int | None = None,
     ) -> None:
         assert path.startswith("/"), "Routed paths must start with '/'"
         self.path = path
@@ -226,6 +228,9 @@ class Route(BaseRoute):
             for cls, args, kwargs in reversed(middleware):
                 self.app = cls(self.app, *args, **kwargs)
 
+        if max_body_size is not None:
+            self.app = RequestBodyLimitMiddleware(self.app, max_body_size)
+
         if methods is None:
             self.methods = None
         else:
@@ -365,6 +370,7 @@ class Mount(BaseRoute):
         name: str | None = None,
         *,
         middleware: Sequence[Middleware] | None = None,
+        max_body_size: int | None = None,
     ) -> None:
         assert path == "" or path.startswith("/"), "Routed paths must start with '/'"
         assert app is not None or routes is not None, "Either 'app=...', or 'routes=' must be specified"
@@ -377,6 +383,8 @@ class Mount(BaseRoute):
         if middleware is not None:
             for cls, args, kwargs in reversed(middleware):
                 self.app = cls(self.app, *args, **kwargs)
+        if max_body_size is not None:
+            self.app = RequestBodyLimitMiddleware(self.app, max_body_size)
         self.name = name
         self.path_regex, self.path_format, self.param_convertors = compile_path(self.path + "/{path:path}")
 
@@ -574,6 +582,7 @@ class Router:
         lifespan: Lifespan[Any] | None = None,
         *,
         middleware: Sequence[Middleware] | None = None,
+        max_body_size: int | None = None,
     ) -> None:
         self.routes = [] if routes is None else list(routes)
         self.redirect_slashes = redirect_slashes
@@ -602,6 +611,8 @@ class Router:
         if middleware:
             for cls, args, kwargs in reversed(middleware):
                 self.middleware_stack = cls(self.middleware_stack, *args, **kwargs)
+        if max_body_size is not None:
+            self.middleware_stack = RequestBodyLimitMiddleware(self.middleware_stack, max_body_size)
 
     async def not_found(self, scope: Scope, receive: Receive, send: Send) -> None:
         if scope["type"] == "websocket":
diff --git a/tests/test_max_body_size.py b/tests/test_max_body_size.py
new file mode 100644
index 0000000..71d4a78
--- /dev/null
+++ b/tests/test_max_body_size.py
@@ -0,0 +1,195 @@
+from __future__ import annotations
+
+from collections.abc import Callable, Iterator
+from tempfile import SpooledTemporaryFile
+from typing import Any
+from unittest import mock
+
+import pytest
+
+from starlette.applications import Starlette
+from starlette.middleware import Middleware
+from starlette.requests import Request
+from starlette.responses import PlainTextResponse
+from starlette.routing import BaseRoute, Mount, Route, Router
+from starlette.types import ASGIApp, Message, Receive, Scope, Send
+from tests.types import TestClientFactory
+
+
+async def read_body(request: Request) -> PlainTextResponse:
+    body = await request.body()
+    return PlainTextResponse(str(len(body)))
+
+
+async def ignore_body(request: Request) -> PlainTextResponse:
+    return PlainTextResponse("ignored")
+
+
+def chunked(size: int) -> Iterator[bytes]:
+    # A generator body is sent without a Content-Length header.
+    yield b"x" * (size // 2)
+    yield b"x" * (size - size // 2)
+
+
+def make_app(kind: str, limit: int | None, endpoint: Callable[..., Any] = read_body) -> Starlette:
+    if kind == "starlette":
+        return Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=limit)
+    if kind == "router":
+        router = Router([Route("/", endpoint, methods=["POST"])], max_body_size=limit)
+        return Starlette(routes=[Mount("/", app=router)])
+    if kind == "mount":
+        return Starlette(routes=[Mount("/", routes=[Route("/", endpoint, methods=["POST"])], max_body_size=limit)])
+    assert kind == "route"
+    return Starlette(routes=[Route("/", endpoint, methods=["POST"], max_body_size=limit)])
+
+
+KINDS = ["starlette", "router", "mount", "route"]
+
+
+@pytest.mark.parametrize("kind", KINDS)
+def test_default_is_unlimited(test_client_factory: TestClientFactory, kind: str) -> None:
+    client = test_client_factory(make_app(kind, None))
+    response = client.post("/", content=b"x" * 100_000)
+    assert response.status_code == 200
+    assert response.text == "100000"
+
+
+@pytest.mark.parametrize("kind", KINDS)
+def test_limit_with_content_length(test_client_factory: TestClientFactory, kind: str) -> None:
+    client = test_client_factory(make_app(kind, 10))
+    assert client.post("/", content=b"x" * 10).text == "10"
+    response = client.post("/", content=b"x" * 11)
+    assert response.status_code == 413
+
+
+@pytest.mark.parametrize("kind", KINDS)
+def test_limit_without_content_length(test_client_factory: TestClientFactory, kind: str) -> None:
+    client = test_client_factory(make_app(kind, 10))
+    assert client.post("/", content=chunked(10)).text == "10"
+    response = client.post("/", content=chunked(11))
+    assert response.status_code == 413
+
+
+@pytest.mark.parametrize("kind", KINDS)
+def test_content_length_fail_fast_when_endpoint_ignores_body(test_client_factory: TestClientFactory, kind: str) -> None:
+    client = test_client_factory(make_app(kind, 10, ignore_body))
+    assert client.post("/", content=b"x" * 10).text == "ignored"
+    assert client.post("/", content=b"x" * 11).status_code == 413
+
+
+class BodyReadingMiddleware:
+    def __init__(self, app: ASGIApp) -> None:
+        self.app = app
+
+    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
+        while (await receive()).get("more_body", False):
+            pass
+        await self.app(scope, receive, send)
+
+
+def test_app_middleware_reading_body_is_limited(test_client_factory: TestClientFactory) -> None:
+    app = Starlette(
+        routes=[Route("/", ignore_body, methods=["POST"])],
+        middleware=[Middleware(BodyReadingMiddleware)],
+        max_body_size=10,
+    )
+    client = test_client_factory(app)
+    assert client.post("/", content=chunked(10)).status_code == 200
+    assert client.post("/", content=chunked(11)).status_code == 413
+
+
+def make_nested_app(kind: str, outer: int, inner: int) -> Starlette:
+    if kind == "router":
+        routes: list[BaseRoute] = [
+            Mount("/", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=inner))
+        ]
+    elif kind == "mount":
+        routes = [Mount("/", routes=[Route("/", read_body, methods=["POST"])], max_body_size=inner)]
+    else:
+        routes = [Route("/", read_body, methods=["POST"], max_body_size=inner)]
+    return Starlette(routes=routes, max_body_size=outer)
+
+
+@pytest.mark.parametrize("kind", ["router", "mount", "route"])
+def test_inner_limit_raises_outer_limit(test_client_factory: TestClientFactory, kind: str) -> None:
+    client = test_client_factory(make_nested_app(kind, outer=10, inner=100))
+    assert client.post("/", content=b"x" * 50).text == "50"
+    assert client.post("/", content=chunked(50)).text == "50"
+    assert client.post("/", content=b"x" * 101).status_code == 413
+    assert client.post("/", content=chunked(101)).status_code == 413
+
+
+@pytest.mark.parametrize("kind", ["router", "mount", "route"])
+def test_inner_limit_lowers_outer_limit(test_client_factory: TestClientFactory, kind: str) -> None:
+    client = test_client_factory(make_nested_app(kind, outer=100, inner=10))
+    assert client.post("/", content=b"x" * 10).text == "10"
+    assert client.post("/", content=b"x" * 50).status_code == 413
+    assert client.post("/", content=chunked(50)).status_code == 413
+
+
+def test_sibling_route_keeps_outer_limit(test_client_factory: TestClientFactory) -> None:
+    app = Starlette(
+        routes=[
+            Route("/big", read_body, methods=["POST"], max_body_size=100),
+            Route("/small", read_body, methods=["POST"]),
+        ],
+        max_body_size=10,
+    )
+    client = test_client_factory(app)
+    assert client.post("/big", content=b"x" * 50).text == "50"
+    assert client.post("/small", content=b"x" * 50).status_code == 413
+
+
+@pytest.mark.anyio
+async def test_multipart_temp_files_closed_on_body_limit() -> None:
+    created: list[SpooledTemporaryFile[bytes]] = []
+
+    def tracking_factory(*args: Any, **kwargs: Any) -> SpooledTemporaryFile[bytes]:
+        file: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(*args, **kwargs)
+        created.append(file)
+        return file
+
+    async def form_endpoint(request: Request) -> PlainTextResponse:
+        async with request.form():
+            return PlainTextResponse("ok")  # pragma: no cover
+
+    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=150)
+
+    chunks = [
+        b'--b\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n'
+        b"Content-Type: text/plain\r\n\r\n" + b"x" * 50,
+        b"x" * 200 + b"\r\n--b--\r\n",
+    ]
+    index = 0
+
+    async def receive() -> Message:
+        nonlocal index
+        body = chunks[index]
+        index += 1
+        return {"type": "http.request", "body": body, "more_body": index < len(chunks)}
+
+    sent: list[Message] = []
+
+    async def send(message: Message) -> None:
+        sent.append(message)
+
+    scope: Scope = {
+        "type": "http",
+        "asgi": {"version": "3.0"},
+        "http_version": "1.1",
+        "method": "POST",
+        "scheme": "http",
+        "path": "/",
+        "raw_path": b"/",
+        "root_path": "",
+        "query_string": b"",
+        "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
+        "server": ("testserver", 80),
+        "client": ("testclient", 123),
+    }
+    with mock.patch("starlette.formparsers.SpooledTemporaryFile", tracking_factory):
+        await app(scope, receive, send)
+
+    assert sent[0]["status"] == 413
+    assert len(created) == 1
+    assert all(file.closed for file in created)

```

## Answer these questions, one at a time, in this order

1. Which acceptance criterion of this task is not met, or is met only in appearance (hard-coded, special-cased, or untested path)?
2. Which relevant case (boundary, error, empty input, interaction with existing options or state) is not covered by any test of this task?
3. Which existing test was weakened: an assertion removed or loosened, a case deleted, a skip added? Look at the modified declared test files above.
4. Which part of the diff could be removed or simplified without losing behaviour the task requires (dead code, duplicated branches, unnecessary abstraction)?

For each question, report only concrete findings: `file`, `line` (in the new version; 0 if the
finding is not tied to a line), the question number as `pass`, a `severity` and a one-sentence
`text`. `blocking` means the task must not be committed as is: a criterion unmet, a relevant case
untested, an existing test weakened, or a change that does not belong to the task. `advisory`
means worth a comment but not worth another attempt. No finding is a valid answer to a question.

## Coverage map

Then fill `coverage` with one entry per acceptance criterion marked `[behavior]` above. For
each, name the single test of this task's test files whose assertion proves the criterion:
`test` as a pytest node id (`tests/path.py::test_name`), `line` of the decisive assertion,
`status` `covered` when that assertion would fail if the criterion were broken, `partial` when
a test touches the behaviour but would not fail (asserts only the happy path, or a weaker
property), `uncovered` when no test exercises it. Read the test body: do not infer coverage
from a test's name. `partial` and `uncovered` are blocking, so when you mark one, say in
`note` what test would prove it.

`verdict` is `request_changes` if and only if there is at least one `blocking` finding or a
criterion that is not `covered`.
