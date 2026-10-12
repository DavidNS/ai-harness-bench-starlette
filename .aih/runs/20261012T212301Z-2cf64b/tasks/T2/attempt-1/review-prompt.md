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

## Task T2 (multipart_cleanup)

In MultiPartParser.parse, close all tracked temporary files on any interruption (for example except BaseException or a try/finally on failure) and re-raise. Add a test with a stream that raises a custom exception mid-file.

Acceptance criteria:
- ACC14 [behavior]: When the stream raises any exception mid-parse after a file part has started, including a body-limit error, MultiPartParser closes every temporary file it created and re-raises.

Files declared for the task:
- starlette/formparsers.py (production)
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
diff --git a/tests/test_formparsers.py b/tests/test_formparsers.py
index 99b2897..8659ac3 100644
--- a/tests/test_formparsers.py
+++ b/tests/test_formparsers.py
@@ -613,6 +613,28 @@ async def test_urlencoded_limits_stop_parsing_within_a_single_chunk() -> None:
     assert sum(len(data) for _, data in parser.messages) <= 1024
 
 
+@pytest.mark.anyio
+async def test_multipart_parser_closes_files_on_any_exception() -> None:
+    class StreamError(Exception):
+        pass
+
+    async def broken_stream() -> AsyncGenerator[bytes, None]:
+        yield (
+            b"--b\r\n"
+            b'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
+            b"Content-Type: text/plain\r\n\r\n"
+            b"partial file data"
+        )
+        raise StreamError("interrupted")
+
+    headers = Headers({"content-type": "multipart/form-data; boundary=b"})
+    parser = MultiPartParser(headers, broken_stream())
+    with pytest.raises(StreamError):
+        await parser.parse()
+    assert len(parser._files_to_close_on_error) == 1
+    assert all(file.closed for file in parser._files_to_close_on_error)
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

`verdict` is `request_changes` if and only if there is at least one `blocking` finding.
