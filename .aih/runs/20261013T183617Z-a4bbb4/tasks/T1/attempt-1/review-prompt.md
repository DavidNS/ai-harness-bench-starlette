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

Add a pure ASGI RequestBodyLimitMiddleware module, fix multipart temp-file cleanup on any interruption, then wire a max_body_size kwarg into Starlette, Router, Mount and Route with outer-to-inner override via the scope key. Three tasks; the middleware and the formparsers fix are independent, and wiring depends on the middleware.

## Task T1 (T1)

Create RequestBodyLimitMiddleware and MAX_BODY_SIZE_SCOPE_KEY in starlette/middleware/body_limit.py with tests. Wrap receive to count bytes; Content-Length fail-fast; track response start to avoid a double response; raise HTTPException(413) from receive; set/restore the scope key.

Acceptance criteria:
- ACC1 [behavior]: test_rejects_body_over_limit_without_content_length: a chunked body exceeding max_body_size gets a plain-text 413 'Content Too Large'.
- ACC2 [behavior]: test_multipart_upload_counts_overhead: multipart upload whose raw body (with boundaries) exceeds the limit is rejected with 413 even if file data alone is under the limit; streaming bodies are counted per chunk.
- ACC3 [behavior]: test_content_length_failfast: a declared Content-Length over the limit gets 413 before receive is called, even if the endpoint never reads the body.
- ACC4 [behavior]: test_understated_content_length_and_exact_limit: an understated Content-Length does not bypass the limit; a body exactly at the limit is accepted.
- ACC5 [behavior]: test_no_double_response: when the limit is crossed after the app started its response, the middleware sends no second response and receive raises HTTPException(413).
- ACC6 [behavior]: test_scope_key_set_and_restored: scope['starlette.max_body_size'] equals the limit inside the app and the previous value (or absence) is restored afterwards, including after errors.
- ACC10 [other]: Middleware is pure ASGI, exports MAX_BODY_SIZE_SCOPE_KEY, and nothing new is exported from starlette/__init__ or the package root.

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
index 0000000..efa2ebb
--- /dev/null
+++ b/starlette/middleware/body_limit.py
@@ -0,0 +1,67 @@
+from __future__ import annotations
+
+from starlette.exceptions import HTTPException
+from starlette.responses import PlainTextResponse
+from starlette.types import ASGIApp, Message, Receive, Scope, Send
+
+MAX_BODY_SIZE_SCOPE_KEY = "starlette.max_body_size"
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
+        response_started = False
+        exceeded = False
+        total = 0
+
+        async def send_wrapper(message: Message) -> None:
+            nonlocal response_started
+            if message["type"] == "http.response.start":
+                response_started = True
+            await send(message)
+
+        async def receive_wrapper() -> Message:
+            nonlocal total, exceeded
+            message = await receive()
+            if message["type"] == "http.request":
+                total += len(message.get("body", b""))
+                if total > self.max_body_size:
+                    exceeded = True
+                    raise HTTPException(status_code=413, detail="Content Too Large")
+            return message
+
+        response = PlainTextResponse("Content Too Large", status_code=413)
+
+        # Content-Length is only a fail-fast hint; the counting in receive is authoritative.
+        for name, value in scope["headers"]:
+            if name == b"content-length":
+                try:
+                    declared = int(value)
+                except ValueError:
+                    break
+                if declared > self.max_body_size:
+                    await response(scope, receive, send)
+                    return
+                break
+
+        missing = object()
+        previous = scope.get(MAX_BODY_SIZE_SCOPE_KEY, missing)
+        scope[MAX_BODY_SIZE_SCOPE_KEY] = self.max_body_size
+        try:
+            await self.app(scope, receive_wrapper, send_wrapper)
+        except HTTPException:
+            if not exceeded or response_started:
+                raise
+            await response(scope, receive, send)
+        finally:
+            if previous is missing:
+                del scope[MAX_BODY_SIZE_SCOPE_KEY]
+            else:
+                scope[MAX_BODY_SIZE_SCOPE_KEY] = previous
diff --git a/tests/middleware/test_body_limit.py b/tests/middleware/test_body_limit.py
new file mode 100644
index 0000000..cd3576d
--- /dev/null
+++ b/tests/middleware/test_body_limit.py
@@ -0,0 +1,221 @@
+from __future__ import annotations
+
+from typing import Any
+
+import pytest
+
+from starlette.exceptions import HTTPException
+from starlette.middleware.body_limit import MAX_BODY_SIZE_SCOPE_KEY, RequestBodyLimitMiddleware
+from starlette.requests import Request
+from starlette.responses import PlainTextResponse
+from starlette.types import Message, Receive, Scope, Send
+from tests.types import TestClientFactory
+
+
+async def read_body_app(scope: Scope, receive: Receive, send: Send) -> None:
+    request = Request(scope, receive)
+    body = b""
+    async for chunk in request.stream():
+        body += chunk
+    await PlainTextResponse(str(len(body)))(scope, receive, send)
+
+
+def make_scope(headers: list[tuple[bytes, bytes]] | None = None, type: str = "http") -> Scope:
+    return {"type": type, "method": "POST", "path": "/", "headers": headers or []}
+
+
+def chunked(*chunks: bytes) -> Receive:
+    messages = [
+        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)
+    ]
+
+    async def receive() -> Message:
+        return messages.pop(0)
+
+    return receive
+
+
+def test_rejects_body_over_limit_without_content_length(test_client_factory: TestClientFactory) -> None:
+    client = test_client_factory(RequestBodyLimitMiddleware(read_body_app, max_body_size=10))
+
+    def gen() -> Any:
+        yield b"123456"
+        yield b"123456"
+
+    response = client.post("/", content=gen())
+    assert response.status_code == 413
+    assert response.text == "Content Too Large"
+    assert response.headers["content-type"].startswith("text/plain")
+
+
+def test_multipart_upload_counts_overhead(test_client_factory: TestClientFactory) -> None:
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        request = Request(scope, receive)
+        async with request.form() as form:
+            upload = form["file"]
+            size = len(await upload.read())  # type: ignore[union-attr]
+        await PlainTextResponse(str(size))(scope, receive, send)
+
+    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=300))
+    ok = client.post("/", files={"file": ("a.txt", b"x" * 10)})
+    assert ok.status_code == 200
+    assert ok.text == "10"
+    # File data (250 bytes) is under the limit, but boundaries and headers push the body over.
+    response = client.post("/", files={"file": ("a.txt", b"x" * 250)})
+    assert response.status_code == 413
+    assert response.text == "Content Too Large"
+
+
+def test_streaming_body_counted_per_chunk(test_client_factory: TestClientFactory) -> None:
+    client = test_client_factory(RequestBodyLimitMiddleware(read_body_app, max_body_size=9))
+
+    def gen(n: int) -> Any:
+        for _ in range(n):
+            yield b"abc"
+
+    assert client.post("/", content=gen(3)).text == "9"
+    assert client.post("/", content=gen(4)).status_code == 413
+
+
+@pytest.mark.anyio
+async def test_content_length_failfast() -> None:
+    async def never_receive() -> Message:
+        raise AssertionError("receive must not be called")  # pragma: no cover
+
+    called = False
+
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        nonlocal called
+        called = True  # pragma: no cover
+
+    sent: list[Message] = []
+
+    async def send(message: Message) -> None:
+        sent.append(message)
+
+    scope = make_scope([(b"content-length", b"11")])
+    await RequestBodyLimitMiddleware(app, max_body_size=10)(scope, never_receive, send)
+    assert not called
+    assert sent[0]["status"] == 413
+    assert sent[1]["body"] == b"Content Too Large"
+    assert MAX_BODY_SIZE_SCOPE_KEY not in scope
+
+
+def test_content_length_failfast_endpoint_never_reads(test_client_factory: TestClientFactory) -> None:
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        await PlainTextResponse("ok")(scope, receive, send)
+
+    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
+    assert client.post("/", content=b"x" * 11).status_code == 413
+    assert client.post("/", content=b"x" * 10).status_code == 200
+
+
+def test_understated_content_length_and_exact_limit(test_client_factory: TestClientFactory) -> None:
+    client = test_client_factory(RequestBodyLimitMiddleware(read_body_app, max_body_size=10))
+    assert client.post("/", content=b"x" * 10).text == "10"
+    # Header claims 5 bytes but 11 are sent.
+    response = client.post("/", content=b"x" * 11, headers={"content-length": "5"})
+    assert response.status_code == 413
+
+
+@pytest.mark.anyio
+async def test_invalid_content_length_is_ignored() -> None:
+    sent: list[Message] = []
+
+    async def send(message: Message) -> None:
+        sent.append(message)
+
+    scope = make_scope([(b"content-length", b"abc")])
+    await RequestBodyLimitMiddleware(read_body_app, max_body_size=10)(scope, chunked(b"x" * 11), send)
+    assert sent[0]["status"] == 413
+
+    sent.clear()
+    await RequestBodyLimitMiddleware(read_body_app, max_body_size=10)(scope, chunked(b"x" * 10), send)
+    assert sent[0]["status"] == 200
+
+
+@pytest.mark.anyio
+async def test_no_double_response() -> None:
+    sent: list[Message] = []
+
+    async def send(message: Message) -> None:
+        sent.append(message)
+
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        await send({"type": "http.response.start", "status": 200, "headers": []})
+        await receive()
+        await receive()  # pragma: no cover
+
+    scope = make_scope()
+    with pytest.raises(HTTPException) as exc_info:
+        await RequestBodyLimitMiddleware(app, max_body_size=5)(scope, chunked(b"x" * 6), send)
+    assert exc_info.value.status_code == 413
+    assert [m["type"] for m in sent] == ["http.response.start"]
+
+
+@pytest.mark.anyio
+async def test_other_http_exception_propagates() -> None:
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        raise HTTPException(status_code=404)
+
+    async def send(message: Message) -> None:
+        raise AssertionError("nothing should be sent")  # pragma: no cover
+
+    with pytest.raises(HTTPException) as exc_info:
+        await RequestBodyLimitMiddleware(app, max_body_size=5)(make_scope(), chunked(b""), send)
+    assert exc_info.value.status_code == 404
+
+
+@pytest.mark.anyio
+async def test_non_http_scope_passes_through() -> None:
+    seen: list[Scope] = []
+
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        seen.append(dict(scope))
+
+    scope = make_scope(type="websocket")
+    await RequestBodyLimitMiddleware(app, max_body_size=5)(scope, chunked(b""), None)  # type: ignore[arg-type]
+    assert MAX_BODY_SIZE_SCOPE_KEY not in seen[0]
+
+
+@pytest.mark.anyio
+async def test_scope_key_set_and_restored() -> None:
+    seen: list[Any] = []
+
+    async def app(scope: Scope, receive: Receive, send: Send) -> None:
+        seen.append(scope[MAX_BODY_SIZE_SCOPE_KEY])
+        if scope["path"] == "/boom":
+            raise RuntimeError("boom")
+
+    async def send(message: Message) -> None:
+        raise AssertionError("nothing should be sent")  # pragma: no cover
+
+    middleware = RequestBodyLimitMiddleware(app, max_body_size=7)
+
+    scope = make_scope()
+    await middleware(scope, chunked(b""), send)
+    assert seen == [7]
+    assert MAX_BODY_SIZE_SCOPE_KEY not in scope
+
+    scope = make_scope()
+    scope[MAX_BODY_SIZE_SCOPE_KEY] = 99
+    await middleware(scope, chunked(b""), send)
+    assert seen[-1] == 7
+    assert scope[MAX_BODY_SIZE_SCOPE_KEY] == 99
+
+    for previous in (None, 99):
+        scope = make_scope()
+        scope["path"] = "/boom"
+        if previous:
+            scope[MAX_BODY_SIZE_SCOPE_KEY] = previous
+        with pytest.raises(RuntimeError):
+            await middleware(scope, chunked(b""), send)
+        assert scope.get(MAX_BODY_SIZE_SCOPE_KEY) == previous
+        assert (MAX_BODY_SIZE_SCOPE_KEY in scope) == bool(previous)
+
+
+def test_not_exported_from_package_root() -> None:
+    import starlette
+
+    assert not hasattr(starlette, "RequestBodyLimitMiddleware")
+    assert not hasattr(starlette, "MAX_BODY_SIZE_SCOPE_KEY")

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
