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

## Task T1 (middleware)

Create RequestBodyLimitMiddleware in starlette/middleware/body_limit.py with tests. It wraps receive and counts bytes, uses Content-Length only as a fail-fast check, and sends a plain-text 413 only if the response has not started. The receive wrapper raises a body-limit error that stops the stream. An inner instance must replace an outer limiter, for example by storing the original receive in a private scope key and unwrapping it, so inner limits can raise or lower the outer one.

Acceptance criteria:
- ACC1 [behavior]: With the middleware and a body over max_body_size sent with a correct Content-Length, the response is 413 text/plain 'Content Too Large' and the app endpoint is not run.
- ACC2 [behavior]: A chunked or streaming body without Content-Length that exceeds the limit gets a 413, counted from bytes actually received.
- ACC3 [behavior]: A body larger than an understated Content-Length still gets 413 once received bytes exceed the limit.
- ACC4 [behavior]: A declared Content-Length above the limit is rejected with 413 without receive() being called.
- ACC5 [behavior]: A body exactly max_body_size bytes is allowed and a body of max_body_size+1 is rejected.
- ACC6 [behavior]: A multipart upload whose total encoded body (boundaries and headers included) exceeds the limit gets 413, while a smaller one succeeds.
- ACC7 [behavior]: If the app has already sent http.response.start when the limit is crossed, the middleware sends no extra response and the app's receive call raises the body-limit error.
- ACC8 [other]: RequestBodyLimitMiddleware is a pure ASGI class, not a BaseHTTPMiddleware subclass, and non-http scopes pass through unchanged.
- ACC15 [behavior]: starlette/__init__.py and starlette.middleware.__init__ have no new names, and RequestBodyLimitMiddleware imports from starlette.middleware.body_limit.

Files declared for the task:
- starlette/middleware/body_limit.py (production)
- tests/middleware/test_body_limit.py (test)

Test files of this task that the change modified (existing tests, declared by the plan):
- (none)

Test results: selected tests green; red proven; suite green

## The diff

```diff
diff --git a/starlette/middleware/body_limit.py b/starlette/middleware/body_limit.py
new file mode 100644
index 0000000..9b875b1
--- /dev/null
+++ b/starlette/middleware/body_limit.py
@@ -0,0 +1,68 @@
+from __future__ import annotations
+
+from starlette.datastructures import Headers
+from starlette.responses import PlainTextResponse
+from starlette.types import ASGIApp, Message, Receive, Scope, Send
+
+_LIMITER_SCOPE_KEY = "starlette.body_limit"
+
+
+class RequestBodyTooLarge(Exception):
+    """Raised from `receive()` when the request body exceeds the configured limit."""
+
+    def __init__(self, max_body_size: int) -> None:
+        super().__init__(f"Request body exceeds the limit of {max_body_size} bytes.")
+        self.max_body_size = max_body_size
+
+
+class RequestBodyLimitMiddleware:
+    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
+        self.app = app
+        self.max_body_size = max_body_size
+
+    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
+        if scope["type"] != "http":
+            await self.app(scope, receive, send)
+            return
+
+        # An inner limiter replaces an outer one, so it can raise or lower the limit.
+        outer = scope.get(_LIMITER_SCOPE_KEY)
+        if outer is not None and outer[0] is receive:
+            receive = outer[1]
+
+        response = PlainTextResponse("Content Too Large", status_code=413)
+
+        # Content-Length is only a fail-fast check, the received bytes are what is enforced.
+        content_length = Headers(scope=scope).get("content-length")
+        if content_length is not None and content_length.isdigit() and int(content_length) > self.max_body_size:
+            await response(scope, receive, send)
+            return
+
+        original_receive = receive
+        max_body_size = self.max_body_size
+        received = 0
+        response_started = False
+
+        async def limited_receive() -> Message:
+            nonlocal received
+            if received > max_body_size:
+                raise RequestBodyTooLarge(max_body_size)
+            message = await original_receive()
+            if message["type"] == "http.request":
+                received += len(message.get("body", b""))
+                if received > max_body_size:
+                    raise RequestBodyTooLarge(max_body_size)
+            return message
+
+        async def tracking_send(message: Message) -> None:
+            nonlocal response_started
+            if message["type"] == "http.response.start":
+                response_started = True
+            await send(message)
+
+        scope[_LIMITER_SCOPE_KEY] = (limited_receive, original_receive)
+        try:
+            await self.app(scope, limited_receive, tracking_send)
+        except RequestBodyTooLarge:
+            if not response_started:
+                await response(scope, original_receive, send)
diff --git a/tests/middleware/test_body_limit.py b/tests/middleware/test_body_limit.py
new file mode 100644
index 0000000..b2d911d
--- /dev/null
+++ b/tests/middleware/test_body_limit.py
@@ -0,0 +1,223 @@
+from __future__ import annotations
+
+from typing import Any
+
+import pytest
+
+from starlette.applications import Starlette
+from starlette.middleware.base import BaseHTTPMiddleware
+from starlette.middleware.body_limit import RequestBodyLimitMiddleware, RequestBodyTooLarge
+from starlette.requests import Request
+from starlette.responses import PlainTextResponse
+from starlette.routing import Route
+from starlette.types import Message, Receive, Scope, Send
+from tests.types import TestClientFactory
+
+
+async def read_all(request: Request) -> PlainTextResponse:
+    body = await request.body()
+    return PlainTextResponse(str(len(body)))
+
+
+async def raw_read_all(scope: Scope, receive: Receive, send: Send) -> None:
+    total = 0
+    more_body = True
+    while more_body:
+        message = await receive()
+        total += len(message.get("body", b""))
+        more_body = message.get("more_body", False)
+    await PlainTextResponse(str(total))(scope, receive, send)
+
+
+def make_app(max_body_size: int) -> RequestBodyLimitMiddleware:
+    return RequestBodyLimitMiddleware(raw_read_all, max_body_size=max_body_size)
+
+
+async def drive(
+    app: Any,
+    chunks: list[bytes],
+    headers: list[tuple[bytes, bytes]] | None = None,
+    scope_type: str = "http",
+) -> tuple[list[Message], list[str]]:
+    """Run an ASGI app with a scripted body; returns sent messages and the names of receive calls."""
+    pending = list(chunks)
+    sent: list[Message] = []
+    calls: list[str] = []
+
+    async def receive() -> Message:
+        calls.append("receive")
+        if pending:
+            body = pending.pop(0)
+            return {"type": "http.request", "body": body, "more_body": bool(pending)}
+        return {"type": "http.disconnect"}
+
+    async def send(message: Message) -> None:
+        sent.append(message)
+
+    scope = {"type": scope_type, "method": "POST", "path": "/", "headers": headers or []}
+    await app(scope, receive, send)
+    return sent, calls
+
+
+def status_of(sent: list[Message]) -> int:
+    return sent[0]["status"]
+
+
+def test_over_limit_with_content_length(test_client_factory: TestClientFactory) -> None:
+    ran = False
+
+    async def endpoint(request: Request) -> PlainTextResponse:
+        nonlocal ran
+        ran = True
+        return PlainTextResponse("ok")  # pragma: no cover
+
+    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", endpoint, methods=["POST"])]), max_body_size=10)
+    client = test_client_factory(app)
+    response = client.post("/", content=b"x" * 11)
+    assert response.status_code == 413
+    assert response.headers["content-type"].startswith("text/plain")
+    assert response.text == "Content Too Large"
+    assert not ran
+
+
+def test_within_limit_and_exactly_at_limit(test_client_factory: TestClientFactory) -> None:
+    client = test_client_factory(make_app(10))
+    assert client.post("/", content=b"x" * 10).text == "10"
+    assert client.post("/", content=b"x" * 3).text == "3"
+    assert client.post("/", content=b"x" * 11).status_code == 413
+
+
+def test_streaming_body_without_content_length(test_client_factory: TestClientFactory) -> None:
+    client = test_client_factory(make_app(10))
+
+    def gen(n: int) -> Any:
+        for _ in range(n):
+            yield b"xxxx"
+
+    assert "content-length" not in client.build_request("POST", "/", content=gen(1)).headers
+    assert client.post("/", content=gen(2)).text == "8"
+    assert client.post("/", content=gen(3)).status_code == 413
+
+
+@pytest.mark.anyio
+async def test_understated_content_length() -> None:
+    sent, _ = await drive(make_app(10), [b"x" * 20], headers=[(b"content-length", b"5")])
+    assert status_of(sent) == 413
+    assert sent[1]["body"] == b"Content Too Large"
+
+
+@pytest.mark.anyio
+async def test_invalid_content_length_is_ignored() -> None:
+    sent, _ = await drive(make_app(10), [b"x" * 4], headers=[(b"content-length", b"abc")])
+    assert status_of(sent) == 200
+    sent, _ = await drive(make_app(10), [b"x" * 40], headers=[(b"content-length", b"abc")])
+    assert status_of(sent) == 413
+
+
+@pytest.mark.anyio
+async def test_declared_content_length_rejected_without_receive() -> None:
+    sent, calls = await drive(make_app(10), [b"x" * 20], headers=[(b"content-length", b"20")])
+    assert status_of(sent) == 413
+    assert calls == []
+
+
+def test_multipart_upload(test_client_factory: TestClientFactory) -> None:
+    async def endpoint(request: Request) -> PlainTextResponse:
+        async with request.form() as form:
+            upload = form["file"]
+            data = await upload.read()  # type: ignore[union-attr]
+        return PlainTextResponse(str(len(data)))
+
+    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", endpoint, methods=["POST"])]), max_body_size=300)
+    client = test_client_factory(app)
+    assert client.post("/", files={"file": ("a.txt", b"x" * 50)}).text == "50"
+    # The file data alone fits in the limit, but boundaries and headers push the body over it.
+    assert client.post("/", files={"file": ("a.txt", b"x" * 290)}).status_code == 413
+
+
+@pytest.mark.anyio
+async def test_no_second_response_when_response_started() -> None:
+    raised: list[BaseException] = []
+
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        await send({"type": "http.response.start", "status": 200, "headers": []})
+        try:
+            while True:
+                await receive()
+        except RequestBodyTooLarge as exc:
+            raised.append(exc)
+            raise
+
+    sent, _ = await drive(RequestBodyLimitMiddleware(app, max_body_size=5), [b"abc", b"def"])
+    assert [m["type"] for m in sent] == ["http.response.start"]
+    assert sent[0]["status"] == 200
+    assert len(raised) == 1
+    assert raised[0].max_body_size == 5  # type: ignore[attr-defined]
+
+
+@pytest.mark.anyio
+async def test_receive_keeps_raising_and_passes_disconnect() -> None:
+    results: list[str] = []
+
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        results.append((await receive())["type"])
+        try:
+            await receive()
+        except RequestBodyTooLarge:
+            results.append("raised")
+        try:
+            await receive()
+        except RequestBodyTooLarge:
+            results.append("raised again")
+            raise
+
+    sent, _ = await drive(RequestBodyLimitMiddleware(app, max_body_size=5), [b"abc", b"defgh"])
+    assert results == ["http.request", "raised", "raised again"]
+    assert status_of(sent) == 413
+
+    results.clear()
+
+    async def disconnect_app(scope: Scope, receive: Receive, send: Send) -> None:
+        results.append((await receive())["type"])
+
+    await drive(RequestBodyLimitMiddleware(disconnect_app, max_body_size=5), [])
+    assert results == ["http.disconnect"]
+
+
+@pytest.mark.anyio
+async def test_inner_limiter_overrides_outer() -> None:
+    raised = RequestBodyLimitMiddleware(make_app(1000), max_body_size=10)
+    sent, _ = await drive(raised, [b"x" * 500])
+    assert status_of(sent) == 200
+
+    lowered = RequestBodyLimitMiddleware(make_app(10), max_body_size=1000)
+    sent, _ = await drive(lowered, [b"x" * 500])
+    assert status_of(sent) == 413
+
+
+@pytest.mark.anyio
+async def test_non_http_scope_passes_through() -> None:
+    seen: list[Any] = []
+
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        seen.append((scope["type"], receive, send))
+
+    middleware = RequestBodyLimitMiddleware(app, max_body_size=1)
+
+    async def receive() -> Message:  # pragma: no cover
+        raise AssertionError
+
+    async def send(message: Message) -> None:  # pragma: no cover
+        raise AssertionError
+
+    await middleware({"type": "websocket"}, receive, send)
+    assert seen == [("websocket", receive, send)]
+
+
+def test_pure_asgi_and_no_new_exports() -> None:
+    import starlette
+    import starlette.middleware
+
+    assert not issubclass(RequestBodyLimitMiddleware, BaseHTTPMiddleware)
+    assert not hasattr(starlette, "RequestBodyLimitMiddleware")
+    assert not hasattr(starlette.middleware, "RequestBodyLimitMiddleware")

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
