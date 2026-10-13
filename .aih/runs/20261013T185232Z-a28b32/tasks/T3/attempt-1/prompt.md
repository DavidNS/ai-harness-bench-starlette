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

## Your task: T3 (T2)

Change RequestBodyLimitMiddleware so the outermost instance defers the Content-Length fail-fast until the effective limit is final. It tracks the innermost limit in the shared state. On the app's first http.response.start, or when the app returns without a response, it replaces the app's response with a 413 if the declared length exceeds that limit. Nested instances keep their own immediate check and update the tracked limit. Add tests.

Acceptance criteria this task must satisfy:
- ACC12 [behavior]: With an outer limit of 10 and an inner limit of 100, a 50-byte POST that carries Content-Length: 50 reaches the endpoint and gets 200.
- ACC13 [behavior]: If the declared Content-Length exceeds the final effective limit, the client gets a plain-text 413 Content Too Large, even when the endpoint never reads the body and even if the app starts a response or returns without sending one. The app's own response is not forwarded, so there is exactly one response.
- ACC14 [behavior]: An absent or understated Content-Length never bypasses the limit. A body exactly at the limit is allowed.
- ACC15 [behavior]: When the limit is crossed after the response has started, no second response is sent and the HTTPException(413) is re-raised.
- ACC16 [behavior]: MAX_BODY_SIZE_SCOPE_KEY holds the effective limit while the app runs, and its previous value or absence is restored afterwards.

Files you may edit (anything else is blocked):
- starlette/middleware/body_limit.py (production, modify)
- tests/middleware/test_body_limit.py (test, modify)

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
