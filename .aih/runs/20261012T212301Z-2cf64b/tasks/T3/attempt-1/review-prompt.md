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
   the body limit must still stop the receive stream.
2. A `max_body_size: int | None = None` keyword on `Starlette`, `Router`, `Mount` and `Route`.
   `None` keeps today's unlimited behaviour. The application-level limit is applied through the
   middleware stack before routing. A limit on an inner route, mount or router overrides the outer
   one after routing, so a route can accept a larger body than the rest of the application.
   Application middleware that reads the body is subject to the application-level limit.
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

Add a pure ASGI RequestBodyLimitMiddleware, make multipart temp-file cleanup cover every interruption, then wire a max_body_size keyword into Starlette, Router, Mount and Route. An inner limit overrides the outer one by unwrapping the outer limiter's receive wrapper. The middleware and the formparsers fix are independent; the wiring depends on the middleware.

## Task T3 (wiring)

Add the max_body_size: int | None = None keyword to Starlette, Router, Mount and Route. Starlette inserts the middleware after ServerErrorMiddleware and before user middleware. Router wraps its middleware_stack core, Mount wraps its app, and Route wraps its app, each only when the value is not None. Add tests for scoping, inner raise and lower, and the default.

Acceptance criteria:
- ACC9 [behavior]: Starlette, Router, Mount and Route accept max_body_size=None, and with the default a large body is accepted unchanged.
- ACC10 [behavior]: Starlette(max_body_size=N) returns 413 for oversized bodies on its routes, including when app middleware reads the body.
- ACC11 [behavior]: max_body_size on Router, Mount and Route each enforces a 413 for requests handled by that component.
- ACC12 [behavior]: An inner Route, Mount or Router limit larger than the Starlette app limit accepts a body that exceeds the app limit.
- ACC13 [behavior]: An inner limit lower than the outer limit rejects a body the outer limit would allow.

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
index 3e1a781..e819762 100644
--- a/starlette/applications.py
+++ b/starlette/applications.py
@@ -5,6 +5,7 @@ from typing import Any, ParamSpec, TypeVar
 
 from starlette.datastructures import State, URLPath
 from starlette.middleware import Middleware, _MiddlewareFactory
+from starlette.middleware.body_limit import RequestBodyLimitMiddleware
 from starlette.middleware.errors import ServerErrorMiddleware
 from starlette.middleware.exceptions import ExceptionMiddleware
 from starlette.requests import Request
@@ -15,6 +16,38 @@ from starlette.types import ASGIApp, ExceptionHandler, Lifespan, Receive, Scope,
 AppType = TypeVar("AppType", bound="Starlette")
 P = ParamSpec("P")
 
+_ORIGINAL_HEADERS_KEY = "starlette.body_limit.headers"
+
+
+class _HideContentLength:
+    """Keeps the app-level limiter from rejecting on `Content-Length` before routing.
+
+    A route, mount or router may raise the limit, so the declared length is only
+    enforced by counting received bytes until `_RestoreContentLength` puts it back.
+    """
+
+    def __init__(self, app: ASGIApp) -> None:
+        self.app = app
+
+    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
+        if scope["type"] != "http":
+            await self.app(scope, receive, send)
+            return
+        headers = scope["headers"]
+        hidden = [(key, value) for key, value in headers if key.lower() != b"content-length"]
+        await self.app({**scope, "headers": hidden, _ORIGINAL_HEADERS_KEY: headers}, receive, send)
+
+
+class _RestoreContentLength:
+    def __init__(self, app: ASGIApp) -> None:
+        self.app = app
+
+    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
+        if scope["type"] != "http":
+            await self.app(scope, receive, send)
+            return
+        await self.app({**scope, "headers": scope[_ORIGINAL_HEADERS_KEY]}, receive, send)
+
 
 class Starlette:
     """Creates an Starlette application."""
@@ -26,6 +59,7 @@ class Starlette:
         middleware: Sequence[Middleware] | None = None,
         exception_handlers: Mapping[Any, ExceptionHandler] | None = None,
         lifespan: Lifespan[AppType] | None = None,
+        max_body_size: int | None = None,
     ) -> None:
         """Initializes the application.
 
@@ -46,12 +80,15 @@ class Starlette:
             lifespan: A lifespan context function, which can be used to perform
                 startup and shutdown tasks. This is a newer style that replaces the
                 `on_startup` and `on_shutdown` handlers. Use one or the other, not both.
+            max_body_size: Maximum size in bytes of a request body. `None` means unlimited.
+                A limit set on a route, mount or router overrides this one.
         """
         self.debug = debug
         self.state = State()
         self.router = Router(routes, lifespan=lifespan)
         self.exception_handlers = {} if exception_handlers is None else dict(exception_handlers)
         self.user_middleware = [] if middleware is None else list(middleware)
+        self.max_body_size = max_body_size
         self.middleware_stack: ASGIApp | None = None
 
     def build_middleware_stack(self) -> ASGIApp:
@@ -65,8 +102,17 @@ class Starlette:
             else:
                 exception_handlers[key] = value
 
+        body_limit: list[Middleware] = []
+        if self.max_body_size is not None:
+            body_limit = [
+                Middleware(_HideContentLength),
+                Middleware(RequestBodyLimitMiddleware, max_body_size=self.max_body_size),
+                Middleware(_RestoreContentLength),
+            ]
+
         middleware = (
             [Middleware(ServerErrorMiddleware, handler=error_handler, debug=debug)]
+            + body_limit
             + self.user_middleware
             + [Middleware(ExceptionMiddleware, handlers=exception_handlers, debug=debug)]
         )
diff --git a/starlette/routing.py b/starlette/routing.py
index a035d18..ccc98f5 100644
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
+            self.app = RequestBodyLimitMiddleware(self.app, max_body_size=max_body_size)
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
+            self.app = RequestBodyLimitMiddleware(self.app, max_body_size=max_body_size)
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
+            self.middleware_stack = RequestBodyLimitMiddleware(self.middleware_stack, max_body_size=max_body_size)
 
     async def not_found(self, scope: Scope, receive: Receive, send: Send) -> None:
         if scope["type"] == "websocket":
diff --git a/tests/test_max_body_size.py b/tests/test_max_body_size.py
new file mode 100644
index 0000000..c8d22fd
--- /dev/null
+++ b/tests/test_max_body_size.py
@@ -0,0 +1,180 @@
+from __future__ import annotations
+
+from collections.abc import Iterator
+from typing import Any
+
+import pytest
+
+from starlette.applications import Starlette
+from starlette.middleware import Middleware
+from starlette.requests import Request
+from starlette.responses import PlainTextResponse, Response
+from starlette.routing import Mount, Route, Router, WebSocketRoute
+from starlette.types import ASGIApp, Message, Receive, Scope, Send
+from starlette.websockets import WebSocket
+from tests.types import TestClientFactory
+
+
+async def read_body(request: Request) -> Response:
+    body = await request.body()
+    return PlainTextResponse(str(len(body)))
+
+
+def chunks(total: int) -> Iterator[bytes]:
+    # A generator body is sent without Content-Length, so only received bytes are counted.
+    for _ in range(total // 10):
+        yield b"x" * 10
+
+
+class ReadBodyMiddleware:
+    def __init__(self, app: ASGIApp) -> None:
+        self.app = app
+
+    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
+        if scope["type"] != "http":  # pragma: no cover
+            await self.app(scope, receive, send)
+            return
+
+        body = await Request(scope, receive).body()
+        sent = False
+
+        async def replay() -> Message:
+            nonlocal sent
+            if sent:
+                return await receive()
+            sent = True
+            return {"type": "http.request", "body": body, "more_body": False}
+
+        await self.app(scope, replay, send)
+
+
+def post(client: Any, path: str, size: int, chunked: bool = False) -> Any:
+    if chunked:
+        return client.post(path, content=chunks(size))
+    return client.post(path, content=b"x" * size)
+
+
+@pytest.mark.parametrize("chunked", [False, True])
+def test_default_is_unlimited(test_client_factory: TestClientFactory, chunked: bool) -> None:
+    app = Starlette(
+        routes=[
+            Route("/", read_body, methods=["POST"]),
+            Mount("/m", routes=[Route("/", read_body, methods=["POST"])]),
+        ],
+        max_body_size=None,
+    )
+    client = test_client_factory(app)
+    assert post(client, "/", 100_000, chunked).text == "100000"
+    assert post(client, "/m/", 100_000, chunked).text == "100000"
+    assert Router(max_body_size=None) is not None
+    assert Mount("/x", routes=[], max_body_size=None) is not None
+    assert Route("/", read_body, max_body_size=None) is not None
+
+
+@pytest.mark.parametrize("chunked", [False, True])
+def test_starlette_limit(test_client_factory: TestClientFactory, chunked: bool) -> None:
+    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
+    client = test_client_factory(app)
+    assert post(client, "/", 100, chunked).text == "100"
+    response = post(client, "/", 200, chunked)
+    assert response.status_code == 413
+    assert response.text == "Content Too Large"
+
+
+@pytest.mark.parametrize("chunked", [False, True])
+def test_starlette_limit_applies_to_middleware_reading_body(
+    test_client_factory: TestClientFactory, chunked: bool
+) -> None:
+    app = Starlette(
+        routes=[Route("/", read_body, methods=["POST"])],
+        middleware=[Middleware(ReadBodyMiddleware)],
+        max_body_size=100,
+    )
+    client = test_client_factory(app)
+    assert post(client, "/", 50, chunked).text == "50"
+    assert post(client, "/", 200, chunked).status_code == 413
+
+
+@pytest.mark.parametrize("chunked", [False, True])
+def test_route_limit(test_client_factory: TestClientFactory, chunked: bool) -> None:
+    app = Starlette(
+        routes=[
+            Route("/limited", read_body, methods=["POST"], max_body_size=100),
+            Route("/free", read_body, methods=["POST"]),
+        ]
+    )
+    client = test_client_factory(app)
+    assert post(client, "/limited", 100, chunked).text == "100"
+    assert post(client, "/limited", 200, chunked).status_code == 413
+    assert post(client, "/free", 200, chunked).text == "200"
+
+
+@pytest.mark.parametrize("chunked", [False, True])
+def test_mount_limit(test_client_factory: TestClientFactory, chunked: bool) -> None:
+    app = Starlette(
+        routes=[
+            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
+            Route("/free", read_body, methods=["POST"]),
+        ]
+    )
+    client = test_client_factory(app)
+    assert post(client, "/m/", 100, chunked).text == "100"
+    assert post(client, "/m/", 200, chunked).status_code == 413
+    assert post(client, "/free", 200, chunked).text == "200"
+
+
+@pytest.mark.parametrize("chunked", [False, True])
+def test_router_limit(test_client_factory: TestClientFactory, chunked: bool) -> None:
+    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
+    client = test_client_factory(router)
+    assert post(client, "/", 100, chunked).text == "100"
+    assert post(client, "/", 200, chunked).status_code == 413
+
+
+@pytest.mark.parametrize("chunked", [False, True])
+def test_router_limit_as_mounted_app(test_client_factory: TestClientFactory, chunked: bool) -> None:
+    inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
+    app = Starlette(routes=[Mount("/r", app=inner)])
+    client = test_client_factory(app)
+    assert post(client, "/r/", 100, chunked).text == "100"
+    assert post(client, "/r/", 200, chunked).status_code == 413
+
+
+@pytest.mark.parametrize("chunked", [False, True])
+@pytest.mark.parametrize("kind", ["route", "mount", "router"])
+def test_inner_limit_raises_outer_limit(test_client_factory: TestClientFactory, chunked: bool, kind: str) -> None:
+    if kind == "route":
+        routes = [Route("/", read_body, methods=["POST"], max_body_size=1000)]
+    elif kind == "mount":
+        routes = [Mount("/", routes=[Route("/", read_body, methods=["POST"])], max_body_size=1000)]
+    else:
+        routes = [Mount("/", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=1000))]
+    client = test_client_factory(Starlette(routes=routes, max_body_size=100))
+    assert post(client, "/", 500, chunked).text == "500"
+    assert post(client, "/", 2000, chunked).status_code == 413
+
+
+def test_non_http_scopes_pass_through(test_client_factory: TestClientFactory) -> None:
+    async def ws_endpoint(websocket: WebSocket) -> None:
+        await websocket.accept()
+        await websocket.send_text("hello")
+        await websocket.close()
+
+    app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)], max_body_size=100)
+    with test_client_factory(app) as client:  # also runs lifespan
+        with client.websocket_connect("/ws") as session:
+            assert session.receive_text() == "hello"
+
+
+@pytest.mark.parametrize("chunked", [False, True])
+@pytest.mark.parametrize("kind", ["route", "mount", "router"])
+def test_inner_limit_lowers_outer_limit(test_client_factory: TestClientFactory, chunked: bool, kind: str) -> None:
+    if kind == "route":
+        routes = [Route("/", read_body, methods=["POST"], max_body_size=10)]
+    elif kind == "mount":
+        routes = [Mount("/", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)]
+    else:
+        routes = [Mount("/", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10))]
+    client = test_client_factory(Starlette(routes=routes, max_body_size=1000))
+    assert post(client, "/", 10, chunked).text == "10"
+    assert post(client, "/", 50, chunked).status_code == 413

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

`verdict` is `request_changes` if and only if there is at least one `blocking` finding.
