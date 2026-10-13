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

Add a pure-ASGI RequestBodyLimitMiddleware in a new module (task 1, with multipart temp-file cleanup in formparsers), then wire a max_body_size keyword into Starlette, Router, Mount and Route (task 2). The middleware shares one counting receive wrapper per request. An inner limited app only updates the scope limit key, which the wrapper reads dynamically, so inner limits can raise or lower the outer one.

## Task T1 (T1)

Create RequestBodyLimitMiddleware with MAX_BODY_SIZE_SCOPE_KEY, a shared counting receive wrapper that reads the effective limit from scope dynamically (so nested limited apps just swap the scope value), Content-Length fail-fast, and no-double-response handling. Make MultiPartParser.parse close its temp files on any BaseException. Add tests.

Acceptance criteria:
- ACC1 [behavior]: test_limit_enforced_without_content_length: a body streamed without Content-Length that exceeds max_body_size gets a plain-text 413 'Content Too Large'; a smaller body gets 200.
- ACC2 [behavior]: test_content_length_failfast: a declared Content-Length over the limit gets 413 before the body is read, even if the endpoint never reads the body.
- ACC3 [behavior]: test_understated_content_length_and_exact_limit: an understated Content-Length does not bypass the limit (413), and a body exactly at the limit is allowed.
- ACC4 [behavior]: test_multipart_and_streaming_counted: multipart uploads are counted including encoding overhead, and streaming (chunked) bodies are limited by cumulative bytes.
- ACC5 [behavior]: test_no_double_response: when the limit is crossed after the app has started its response, no second response start is sent and receive raises HTTPException(413); before the response starts the 413 is produced.
- ACC6 [behavior]: test_scope_key: MAX_BODY_SIZE_SCOPE_KEY == 'starlette.max_body_size'; the scope holds the effective limit while the app runs and the previous value (or absence) is restored afterwards.
- ACC7 [behavior]: test_multipart_temp_files_closed_on_interrupt: when the multipart stream raises any exception (e.g. HTTPException 413) mid-stream, all SpooledTemporaryFiles already created are closed.
- ACC11 [other]: The middleware is not exported from starlette/__init__.py and the existing test suite remains green.

Files declared for the task:
- starlette/middleware/body_limit.py (production)
- starlette/formparsers.py (production)
- tests/middleware/test_body_limit.py (test)
- tests/test_formparsers.py (test)

Test files of this task that the change modified (existing tests, declared by the plan):
- tests/test_formparsers.py

Test results: selected tests green; red proven; suite green

## The diff

```diff
diff --git a/starlette/formparsers.py b/starlette/formparsers.py
index 3f3c598..8a5d3fc 100644
--- a/starlette/formparsers.py
+++ b/starlette/formparsers.py
@@ -288,10 +288,10 @@ class MultiPartParser:
                 self._file_parts_to_write.clear()
                 self._file_parts_to_finish.clear()
             parser.finalize()
-        except (MultiPartException, OSError) as exc:
-            # Close all the files if there was an error.
+        except BaseException:
+            # Close all the files if the parse was interrupted for any reason.
             for file in self._files_to_close_on_error:
                 file.close()
-            raise exc
+            raise
 
         return FormData(self.items)
diff --git a/starlette/middleware/body_limit.py b/starlette/middleware/body_limit.py
new file mode 100644
index 0000000..7f3a244
--- /dev/null
+++ b/starlette/middleware/body_limit.py
@@ -0,0 +1,90 @@
+from __future__ import annotations
+
+from starlette.datastructures import Headers
+from starlette.exceptions import HTTPException
+from starlette.responses import PlainTextResponse
+from starlette.types import ASGIApp, Message, Receive, Scope, Send
+
+MAX_BODY_SIZE_SCOPE_KEY = "starlette.max_body_size"
+_STATE_SCOPE_KEY = "starlette.max_body_size_state"
+_MISSING = object()
+
+
+class _BodyState:
+    def __init__(self) -> None:
+        self.total = 0
+        self.exceeded = False
+        self.response_started = False
+
+
+class RequestBodyLimitMiddleware:
+    """
+    Limit the total size of the request body, as received from the ASGI server.
+
+    The effective limit lives in `scope[MAX_BODY_SIZE_SCOPE_KEY]`. The counting
+    `receive` wrapper reads it dynamically, so a nested limited app only needs
+    to swap the scope value to raise or lower the limit for its own scope.
+    """
+
+    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
+        self.app = app
+        self.max_body_size = max_body_size
+
+    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
+        if scope["type"] != "http":
+            await self.app(scope, receive, send)
+            return
+
+        state: _BodyState | None = scope.get(_STATE_SCOPE_KEY)
+        owner = state is None
+        if state is None:
+            state = _BodyState()
+            scope[_STATE_SCOPE_KEY] = state
+        previous = scope.get(MAX_BODY_SIZE_SCOPE_KEY, _MISSING)
+        scope[MAX_BODY_SIZE_SCOPE_KEY] = self.max_body_size
+        try:
+            content_length = Headers(scope=scope).get("content-length")
+            declared = int(content_length) if content_length is not None and content_length.isdigit() else 0
+            too_large = declared > self.max_body_size
+            if not owner:
+                # Counting is already done by the outermost limited app.
+                if too_large:
+                    state.exceeded = True
+                    raise HTTPException(status_code=413)
+                await self.app(scope, receive, send)
+                return
+
+            if too_large:
+                await self._reject(scope, receive, send)
+                return
+
+            async def limited_receive() -> Message:
+                message = await receive()
+                if message["type"] == "http.request":
+                    state.total += len(message.get("body", b""))
+                    if state.total > scope[MAX_BODY_SIZE_SCOPE_KEY]:
+                        state.exceeded = True
+                        raise HTTPException(status_code=413)
+                return message
+
+            async def tracking_send(message: Message) -> None:
+                if message["type"] == "http.response.start":
+                    state.response_started = True
+                await send(message)
+
+            try:
+                await self.app(scope, limited_receive, tracking_send)
+            except HTTPException as exc:
+                if exc.status_code != 413 or not state.exceeded or state.response_started:
+                    raise
+                await self._reject(scope, receive, send)
+        finally:
+            if previous is _MISSING:
+                del scope[MAX_BODY_SIZE_SCOPE_KEY]
+            else:
+                scope[MAX_BODY_SIZE_SCOPE_KEY] = previous
+            if owner:
+                del scope[_STATE_SCOPE_KEY]
+
+    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
+        await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)
diff --git a/tests/middleware/test_body_limit.py b/tests/middleware/test_body_limit.py
new file mode 100644
index 0000000..f03d21e
--- /dev/null
+++ b/tests/middleware/test_body_limit.py
@@ -0,0 +1,233 @@
+from __future__ import annotations
+
+from collections.abc import Iterator
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
+async def read_all(scope: Scope, receive: Receive, send: Send) -> None:
+    total = 0
+    while True:
+        message = await receive()
+        total += len(message.get("body", b""))
+        if not message.get("more_body", False):
+            break
+    await PlainTextResponse(str(total))(scope, receive, send)
+
+
+async def never_reads(scope: Scope, receive: Receive, send: Send) -> None:
+    never_reads.called = True  # type: ignore[attr-defined]
+    await PlainTextResponse("ok")(scope, receive, send)
+
+
+async def run(
+    app: object,
+    chunks: list[bytes],
+    headers: list[tuple[bytes, bytes]] | None = None,
+    scope_extra: dict[str, object] | None = None,
+) -> tuple[list[Message], Scope]:
+    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": headers or []}
+    scope.update(scope_extra or {})
+    pending: list[Message] = [
+        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)
+    ]
+    sent: list[Message] = []
+
+    async def receive() -> Message:
+        if pending:
+            return pending.pop(0)
+        return {"type": "http.disconnect"}
+
+    async def send(message: Message) -> None:
+        sent.append(message)
+
+    await app(scope, receive, send)  # type: ignore[operator]
+    return sent, scope
+
+
+def status_of(sent: list[Message]) -> int:
+    return next(m["status"] for m in sent if m["type"] == "http.response.start")
+
+
+def body_of(sent: list[Message]) -> bytes:
+    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
+
+
+def test_limit_enforced_without_content_length(test_client_factory: TestClientFactory) -> None:
+    client = test_client_factory(RequestBodyLimitMiddleware(read_all, max_body_size=10))
+
+    def gen(n: int) -> Iterator[bytes]:
+        yield b"x" * n
+
+    response = client.post("/", content=gen(11))
+    assert response.status_code == 413
+    assert response.text == "Content Too Large"
+    assert response.headers["content-type"].startswith("text/plain")
+
+    response = client.post("/", content=gen(5))
+    assert response.status_code == 200
+    assert response.text == "5"
+
+
+def test_content_length_failfast(test_client_factory: TestClientFactory) -> None:
+    never_reads.called = False  # type: ignore[attr-defined]
+    client = test_client_factory(RequestBodyLimitMiddleware(never_reads, max_body_size=10))
+
+    response = client.post("/", content=b"x" * 20)
+    assert response.status_code == 413
+    assert response.text == "Content Too Large"
+    assert never_reads.called is False  # type: ignore[attr-defined]
+
+    response = client.post("/", content=b"x" * 5)
+    assert response.status_code == 200
+    assert never_reads.called is True  # type: ignore[attr-defined]
+
+
+@pytest.mark.anyio
+async def test_understated_content_length_and_exact_limit() -> None:
+    app = RequestBodyLimitMiddleware(read_all, max_body_size=10)
+
+    sent, _ = await run(app, [b"x" * 20], headers=[(b"content-length", b"5")])
+    assert status_of(sent) == 413
+    assert body_of(sent) == b"Content Too Large"
+
+    sent, _ = await run(app, [b"x" * 10], headers=[(b"content-length", b"10")])
+    assert status_of(sent) == 200
+    assert body_of(sent) == b"10"
+
+    # An unparsable Content-Length is ignored by the fail-fast check, the body is still counted.
+    sent, _ = await run(app, [b"x" * 11], headers=[(b"content-length", b"abc")])
+    assert status_of(sent) == 413
+
+
+def test_multipart_and_streaming_counted(test_client_factory: TestClientFactory) -> None:
+    async def form_app(scope: Scope, receive: Receive, send: Send) -> None:
+        async with Request(scope, receive).form() as form:
+            size = len(await form["file"].read())  # type: ignore[union-attr]
+        await PlainTextResponse(str(size))(scope, receive, send)
+
+    # The file content alone is exactly at the limit; multipart overhead pushes the request over it.
+    client = test_client_factory(RequestBodyLimitMiddleware(form_app, max_body_size=100))
+    response = client.post("/", files={"file": ("a.txt", b"x" * 100)})
+    assert response.status_code == 413
+    assert response.text == "Content Too Large"
+
+    client = test_client_factory(RequestBodyLimitMiddleware(form_app, max_body_size=1000))
+    response = client.post("/", files={"file": ("a.txt", b"x" * 100)})
+    assert response.status_code == 200
+    assert response.text == "100"
+
+    def chunks(n: int) -> Iterator[bytes]:
+        for _ in range(n):
+            yield b"abcd"
+
+    client = test_client_factory(RequestBodyLimitMiddleware(read_all, max_body_size=10))
+    assert client.post("/", content=chunks(2)).status_code == 200
+    assert client.post("/", content=chunks(3)).status_code == 413
+
+
+@pytest.mark.anyio
+async def test_no_double_response() -> None:
+    async def respond_then_read(scope: Scope, receive: Receive, send: Send) -> None:
+        await send({"type": "http.response.start", "status": 200, "headers": []})
+        await receive()
+        await send({"type": "http.response.body", "body": b"unreachable"})
+
+    app = RequestBodyLimitMiddleware(respond_then_read, max_body_size=3)
+    sent: list[Message] = []
+
+    async def receive() -> Message:
+        return {"type": "http.request", "body": b"abcd", "more_body": False}
+
+    async def send(message: Message) -> None:
+        sent.append(message)
+
+    with pytest.raises(HTTPException) as exc_info:
+        await app({"type": "http", "headers": []}, receive, send)
+    assert exc_info.value.status_code == 413
+    assert [m["type"] for m in sent] == ["http.response.start"]
+
+    # Before the response starts, the 413 is produced by the middleware.
+    sent, _ = await run(RequestBodyLimitMiddleware(read_all, max_body_size=3), [b"abcd"])
+    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
+    assert status_of(sent) == 413
+
+
+@pytest.mark.anyio
+async def test_other_http_exceptions_and_non_http_scopes_pass_through() -> None:
+    async def not_found(scope: Scope, receive: Receive, send: Send) -> None:
+        raise HTTPException(status_code=404)
+
+    with pytest.raises(HTTPException) as exc_info:
+        await run(RequestBodyLimitMiddleware(not_found, max_body_size=3), [b"a"])
+    assert exc_info.value.status_code == 404
+
+    seen: list[Scope] = []
+
+    async def ws_app(scope: Scope, receive: Receive, send: Send) -> None:
+        seen.append(dict(scope))
+
+    middleware = RequestBodyLimitMiddleware(ws_app, max_body_size=3)
+
+    async def receive() -> Message:  # pragma: no cover
+        raise AssertionError
+
+    async def send(message: Message) -> None:  # pragma: no cover
+        raise AssertionError
+
+    await middleware({"type": "websocket"}, receive, send)
+    assert seen == [{"type": "websocket"}]
+
+
+@pytest.mark.anyio
+async def test_scope_key() -> None:
+    assert MAX_BODY_SIZE_SCOPE_KEY == "starlette.max_body_size"
+    observed: list[object] = []
+
+    async def spy(scope: Scope, receive: Receive, send: Send) -> None:
+        observed.append(scope.get(MAX_BODY_SIZE_SCOPE_KEY))
+        await PlainTextResponse("ok")(scope, receive, send)
+
+    app = RequestBodyLimitMiddleware(spy, max_body_size=7)
+
+    _, scope = await run(app, [b""])
+    assert observed == [7]
+    assert MAX_BODY_SIZE_SCOPE_KEY not in scope
+
+    _, scope = await run(app, [b""], scope_extra={MAX_BODY_SIZE_SCOPE_KEY: 99})
+    assert observed == [7, 7]
+    assert scope[MAX_BODY_SIZE_SCOPE_KEY] == 99
+
+
+@pytest.mark.anyio
+async def test_nested_limits_raise_and_lower_the_outer_limit() -> None:
+    inner_calls: list[object] = []
+
+    async def spy_read_all(scope: Scope, receive: Receive, send: Send) -> None:
+        inner_calls.append(scope[MAX_BODY_SIZE_SCOPE_KEY])
+        await read_all(scope, receive, send)
+
+    # Inner limit raises the outer one.
+    raised = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(spy_read_all, max_body_size=100), max_body_size=5)
+    sent, scope = await run(raised, [b"x" * 50])
+    assert status_of(sent) == 200
+    assert body_of(sent) == b"50"
+    assert MAX_BODY_SIZE_SCOPE_KEY not in scope
+
+    # Inner limit lowers the outer one.
+    lowered = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(spy_read_all, max_body_size=5), max_body_size=100)
+    sent, _ = await run(lowered, [b"x" * 50])
+    assert status_of(sent) == 413
+
+    # Inner Content-Length fail-fast does not call the app.
+    inner_calls.clear()
+    sent, _ = await run(lowered, [b"x" * 50], headers=[(b"content-length", b"50")])
+    assert status_of(sent) == 413
+    assert inner_calls == []
diff --git a/tests/test_formparsers.py b/tests/test_formparsers.py
index 99b2897..036c2fe 100644
--- a/tests/test_formparsers.py
+++ b/tests/test_formparsers.py
@@ -613,6 +613,38 @@ async def test_urlencoded_limits_stop_parsing_within_a_single_chunk() -> None:
     assert sum(len(data) for _, data in parser.messages) <= 1024
 
 
+@pytest.mark.anyio
+async def test_multipart_temp_files_closed_on_interrupt() -> None:
+    from starlette.exceptions import HTTPException
+
+    created: list[SpooledTemporaryFile[bytes]] = []
+
+    def tracking_factory(*args: Any, **kwargs: Any) -> SpooledTemporaryFile[bytes]:
+        file: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(*args, **kwargs)
+        created.append(file)
+        return file
+
+    async def interrupted_stream() -> AsyncGenerator[bytes, None]:
+        yield (
+            b"--b\r\n"
+            b'Content-Disposition: form-data; name="a"; filename="a.txt"\r\n\r\n'
+            b"hello\r\n"
+            b"--b\r\n"
+            b'Content-Disposition: form-data; name="c"; filename="c.txt"\r\n\r\n'
+            b"world"
+        )
+        raise HTTPException(status_code=413)
+
+    headers = Headers({"content-type": "multipart/form-data; boundary=b"})
+    parser = MultiPartParser(headers, interrupted_stream())
+    with mock.patch("starlette.formparsers.SpooledTemporaryFile", tracking_factory):
+        with pytest.raises(HTTPException) as exc_info:
+            await parser.parse()
+    assert exc_info.value.status_code == 413
+    assert len(created) == 2
+    assert all(file.closed for file in created)
+
+
 def test_user_safe_decode_helper() -> None:
     result = _user_safe_decode(b"\xc4\x99\xc5\xbc\xc4\x87", "utf-8")
     assert result == "ężć"

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
