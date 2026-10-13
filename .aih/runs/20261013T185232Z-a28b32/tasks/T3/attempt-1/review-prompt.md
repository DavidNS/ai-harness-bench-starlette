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

## Task T3 (T2)

Change RequestBodyLimitMiddleware so the outermost instance defers the Content-Length fail-fast until the effective limit is final. It tracks the innermost limit in the shared state. On the app's first http.response.start, or when the app returns without a response, it replaces the app's response with a 413 if the declared length exceeds that limit. Nested instances keep their own immediate check and update the tracked limit. Add tests.

Acceptance criteria:
- ACC12 [behavior]: With an outer limit of 10 and an inner limit of 100, a 50-byte POST that carries Content-Length: 50 reaches the endpoint and gets 200.
- ACC13 [behavior]: If the declared Content-Length exceeds the final effective limit, the client gets a plain-text 413 Content Too Large, even when the endpoint never reads the body and even if the app starts a response or returns without sending one. The app's own response is not forwarded, so there is exactly one response.
- ACC14 [behavior]: An absent or understated Content-Length never bypasses the limit. A body exactly at the limit is allowed.
- ACC15 [behavior]: When the limit is crossed after the response has started, no second response is sent and the HTTPException(413) is re-raised.
- ACC16 [behavior]: MAX_BODY_SIZE_SCOPE_KEY holds the effective limit while the app runs, and its previous value or absence is restored afterwards.

Files declared for the task:
- starlette/middleware/body_limit.py (production)
- tests/middleware/test_body_limit.py (test)

Test files of this task that the change modified (existing tests, declared by the plan):
- tests/middleware/test_body_limit.py

Test results: selected tests green; red proven; suite green

## The diff

```diff
diff --git a/starlette/middleware/body_limit.py b/starlette/middleware/body_limit.py
index 7f3a244..c81e29c 100644
--- a/starlette/middleware/body_limit.py
+++ b/starlette/middleware/body_limit.py
@@ -15,6 +15,8 @@ class _BodyState:
         self.total = 0
         self.exceeded = False
         self.response_started = False
+        self.replaced = False
+        self.limit = 0
 
 
 class RequestBodyLimitMiddleware:
@@ -45,19 +47,15 @@ class RequestBodyLimitMiddleware:
         try:
             content_length = Headers(scope=scope).get("content-length")
             declared = int(content_length) if content_length is not None and content_length.isdigit() else 0
-            too_large = declared > self.max_body_size
+            state.limit = self.max_body_size
             if not owner:
                 # Counting is already done by the outermost limited app.
-                if too_large:
+                if declared > self.max_body_size:
                     state.exceeded = True
                     raise HTTPException(status_code=413)
                 await self.app(scope, receive, send)
                 return
 
-            if too_large:
-                await self._reject(scope, receive, send)
-                return
-
             async def limited_receive() -> Message:
                 message = await receive()
                 if message["type"] == "http.request":
@@ -68,12 +66,21 @@ class RequestBodyLimitMiddleware:
                 return message
 
             async def tracking_send(message: Message) -> None:
+                if state.replaced:
+                    return
                 if message["type"] == "http.response.start":
                     state.response_started = True
+                    if declared > state.limit:
+                        # The limit is final once the app answers: replace its response.
+                        state.replaced = True
+                        await self._reject(scope, receive, send)
+                        return
                 await send(message)
 
             try:
                 await self.app(scope, limited_receive, tracking_send)
+                if not state.response_started and declared > state.limit:
+                    await self._reject(scope, receive, send)
             except HTTPException as exc:
                 if exc.status_code != 413 or not state.exceeded or state.response_started:
                     raise
diff --git a/tests/middleware/test_body_limit.py b/tests/middleware/test_body_limit.py
index f03d21e..2a35bf4 100644
--- a/tests/middleware/test_body_limit.py
+++ b/tests/middleware/test_body_limit.py
@@ -83,13 +83,43 @@ def test_content_length_failfast(test_client_factory: TestClientFactory) -> None
     response = client.post("/", content=b"x" * 20)
     assert response.status_code == 413
     assert response.text == "Content Too Large"
-    assert never_reads.called is False  # type: ignore[attr-defined]
+    # The endpoint runs, but its own response is replaced by the 413.
+    assert never_reads.called is True  # type: ignore[attr-defined]
 
+    never_reads.called = False  # type: ignore[attr-defined]
     response = client.post("/", content=b"x" * 5)
     assert response.status_code == 200
+    assert response.text == "ok"
     assert never_reads.called is True  # type: ignore[attr-defined]
 
 
+@pytest.mark.anyio
+async def test_content_length_failfast_sends_exactly_one_response() -> None:
+    sent, _ = await run(
+        RequestBodyLimitMiddleware(never_reads, max_body_size=10),
+        [b"x" * 20],
+        headers=[(b"content-length", b"20")],
+    )
+    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
+    assert status_of(sent) == 413
+    assert body_of(sent) == b"Content Too Large"
+
+
+@pytest.mark.anyio
+async def test_content_length_failfast_when_app_returns_without_response() -> None:
+    async def silent(scope: Scope, receive: Receive, send: Send) -> None:
+        pass
+
+    app = RequestBodyLimitMiddleware(silent, max_body_size=10)
+
+    sent, _ = await run(app, [b"x" * 20], headers=[(b"content-length", b"20")])
+    assert status_of(sent) == 413
+    assert body_of(sent) == b"Content Too Large"
+
+    sent, _ = await run(app, [b"x" * 5], headers=[(b"content-length", b"5")])
+    assert sent == []
+
+
 @pytest.mark.anyio
 async def test_understated_content_length_and_exact_limit() -> None:
     app = RequestBodyLimitMiddleware(read_all, max_body_size=10)
@@ -221,6 +251,14 @@ async def test_nested_limits_raise_and_lower_the_outer_limit() -> None:
     assert body_of(sent) == b"50"
     assert MAX_BODY_SIZE_SCOPE_KEY not in scope
 
+    # The declared Content-Length is checked against the final (inner) limit.
+    inner_calls.clear()
+    raised = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(spy_read_all, max_body_size=100), max_body_size=10)
+    sent, _ = await run(raised, [b"x" * 50], headers=[(b"content-length", b"50")])
+    assert status_of(sent) == 200
+    assert body_of(sent) == b"50"
+    assert inner_calls == [100]
+
     # Inner limit lowers the outer one.
     lowered = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(spy_read_all, max_body_size=5), max_body_size=100)
     sent, _ = await run(lowered, [b"x" * 50])

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
