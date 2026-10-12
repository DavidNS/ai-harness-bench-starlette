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

Add a pure ASGI RequestBodyLimitMiddleware that keeps shared limit state in the scope so inner Route/Mount/Router limits can override the outer one. Make the multipart parser close temp files on any interruption. Then wire max_body_size into Router/Mount/Route and into Starlette's middleware stack. Four tasks; docs are not added.

## Your task: T1 (middleware)

Create RequestBodyLimitMiddleware. It stores shared state in scope (limit, received count, response-started flag), so a nested instance overrides the limit instead of double counting. It does the Content-Length fail-fast check, counts received bytes, sends a plain-text 413 only if no response has started, and always raises or stops the receive stream.

Acceptance criteria this task must satisfy:
- ACC1 [behavior]: A body larger than max_body_size, with or without Content-Length, gets a plain-text 413 'Content Too Large' response; streaming bodies and multipart uploads are counted by received bytes including encoding overhead.
- ACC2 [behavior]: A declared Content-Length over the limit is rejected with 413 before the app reads the body. An understated Content-Length does not bypass the limit. A body exactly at the limit succeeds.
- ACC3 [behavior]: If the app already sent http.response.start when the limit is crossed, the middleware sends no second response, and the receive call still raises or terminates.
- ACC7 [other]: Nothing new is exported from the starlette package root; the middleware is imported from starlette.middleware.body_limit.

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
