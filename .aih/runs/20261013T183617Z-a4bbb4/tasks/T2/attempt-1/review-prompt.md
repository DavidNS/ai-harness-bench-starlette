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

## Task T2 (T2)

In MultiPartParser.parse, close temp files on any exception (except BaseException) rather than only MultiPartException/OSError, then re-raise; add a test.

Acceptance criteria:
- ACC9 [behavior]: test_multipart_closes_tempfiles_on_interrupt: temp files created during multipart parsing are closed when parsing is interrupted by any exception, including HTTPException(413) raised from the stream mid-parse.

Files declared for the task:
- starlette/formparsers.py (production)
- tests/test_formparsers.py (test)

Test files of this task that the change modified (existing tests, declared by the plan):
- tests/test_formparsers.py

Test results: selected tests green; red proven; suite green

## The diff

```diff
diff --git a/starlette/formparsers.py b/starlette/formparsers.py
index 3f3c598..8529e8a 100644
--- a/starlette/formparsers.py
+++ b/starlette/formparsers.py
@@ -288,10 +288,10 @@ class MultiPartParser:
                 self._file_parts_to_write.clear()
                 self._file_parts_to_finish.clear()
             parser.finalize()
-        except (MultiPartException, OSError) as exc:
-            # Close all the files if there was an error.
+        except Exception:
+            # Close all the files if the parse was interrupted for any reason.
             for file in self._files_to_close_on_error:
                 file.close()
-            raise exc
+            raise
 
         return FormData(self.items)
diff --git a/tests/test_formparsers.py b/tests/test_formparsers.py
index 99b2897..f6893a6 100644
--- a/tests/test_formparsers.py
+++ b/tests/test_formparsers.py
@@ -14,6 +14,7 @@ import pytest
 
 from starlette.applications import Starlette
 from starlette.datastructures import Headers, UploadFile
+from starlette.exceptions import HTTPException
 from starlette.formparsers import FormParser, MultiPartException, MultiPartParser, _user_safe_decode
 from starlette.requests import Request
 from starlette.responses import JSONResponse
@@ -850,6 +851,21 @@ def test_max_files_is_customizable_low_raises(
         assert res.text == "Too many files. Maximum number of files is 1."
 
 
+@pytest.mark.anyio
+async def test_multipart_closes_tempfiles_on_interrupt() -> None:
+    async def stream() -> AsyncGenerator[bytes, None]:
+        yield b'--B\r\nContent-Disposition: form-data; name="f"; filename="f.txt";\r\n\r\npartial data'
+        raise HTTPException(status_code=413)
+
+    headers = Headers({"Content-Type": "multipart/form-data; boundary=B"})
+    parser = MultiPartParser(headers, stream())
+    with pytest.raises(HTTPException) as exc_info:
+        await parser.parse()
+    assert exc_info.value.status_code == 413
+    assert len(parser._files_to_close_on_error) == 1
+    assert all(file.closed for file in parser._files_to_close_on_error)
+
+
 def test_max_fields_is_customizable_high(test_client_factory: TestClientFactory) -> None:
     client = test_client_factory(make_app_max_parts(max_fields=2000, max_files=2000))
     fields = []

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
