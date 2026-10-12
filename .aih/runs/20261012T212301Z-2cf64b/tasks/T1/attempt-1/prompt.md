You are implementing one task of an approved plan, in a fresh session, inside an isolated
worktree. You can read and edit files but not run commands: the harness runs the tests for you
when you try to finish and gives you the result. Edits outside the files listed for this task are
rejected by a hook; do not work around it.

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

## Your task: T1 (middleware)

Create RequestBodyLimitMiddleware in starlette/middleware/body_limit.py with tests. It wraps receive and counts bytes, uses Content-Length only as a fail-fast check, and sends a plain-text 413 only if the response has not started. The receive wrapper raises a body-limit error that stops the stream. An inner instance must replace an outer limiter, for example by storing the original receive in a private scope key and unwrapping it, so inner limits can raise or lower the outer one.

Acceptance criteria this task must satisfy:
- ACC1 [behavior]: With the middleware and a body over max_body_size sent with a correct Content-Length, the response is 413 text/plain 'Content Too Large' and the app endpoint is not run.
- ACC2 [behavior]: A chunked or streaming body without Content-Length that exceeds the limit gets a 413, counted from bytes actually received.
- ACC3 [behavior]: A body larger than an understated Content-Length still gets 413 once received bytes exceed the limit.
- ACC4 [behavior]: A declared Content-Length above the limit is rejected with 413 without receive() being called.
- ACC5 [behavior]: A body exactly max_body_size bytes is allowed and a body of max_body_size+1 is rejected.
- ACC6 [behavior]: A multipart upload whose total encoded body (boundaries and headers included) exceeds the limit gets 413, while a smaller one succeeds.
- ACC7 [behavior]: If the app has already sent http.response.start when the limit is crossed, the middleware sends no extra response and the app's receive call raises the body-limit error.
- ACC8 [other]: RequestBodyLimitMiddleware is a pure ASGI class, not a BaseHTTPMiddleware subclass, and non-http scopes pass through unchanged.
- ACC15 [behavior]: starlette/__init__.py and starlette.middleware.__init__ have no new names, and RequestBodyLimitMiddleware imports from starlette.middleware.body_limit.

Files you may edit (anything else is blocked):
- starlette/middleware/body_limit.py (production, create)
- tests/middleware/test_body_limit.py (test, create)

Tests that must pass when you finish: tests/middleware/test_body_limit.py

## Rules

- Write or update the tests listed above so that they fail without your production change and
  pass with it. The harness verifies this against the base commit: a test that also passes
  without the change does not count.
- Every branch you add (a new `if`/`else`, an `except`, an early return, a new error status)
  must be exercised by a test in this task's test files before you finish. An independent
  reviewer blocks the task for each new branch without a test; write them first.
- Do not modify existing tests other than the ones listed for this task.
- Do not add features, docs or refactors the task does not ask for.
- If the task cannot be done without touching a file that is not listed, or a criterion is
  impossible as written, do not improvise: stop and return `replan_request` with the reason and the
  files you need. That re-plans instead of failing you.


## Final answer

Return JSON matching the schema you were given: a short `summary`, one `criteria_notes` entry per
criterion with the test that proves it, and `replan_request` (`null` unless you need a re-plan).
