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

## Your task: T4 (T3)

Add the max_body_size keyword to Starlette, Router, Mount and Route. Starlette inserts the middleware after ServerErrorMiddleware and before user middleware. Route, Mount and Router wrap their app outermost, only when the value is not None. Add tests/test_max_body_size.py for all four classes, including inner limits that raise and lower the outer one, app middleware that reads the body, and multipart temp-file cleanup under a limit.

Acceptance criteria this task must satisfy:
- ACC17 [behavior]: Starlette, Router, Mount and Route accept max_body_size. None, the default, leaves the body unlimited, and an oversized body is rejected with 413 when a limit is set.
- ACC18 [behavior]: Application middleware that reads the body is subject to the Starlette(max_body_size=...) limit, and an oversized body gets 413.
- ACC19 [behavior]: A limit on an inner Route, Mount or Router overrides the outer limit. It can raise the limit so a larger body is accepted, or lower it so a body the outer limit allows is rejected.
- ACC20 [behavior]: Temp files created by multipart parsing are closed when parsing is interrupted, including by a body-limit error.
- ACC21 [other]: Tests exist in tests/middleware/test_body_limit.py and tests/test_max_body_size.py covering the listed scenarios, and the existing suite stays green.

Files you may edit (anything else is blocked):
- starlette/applications.py (production, modify)
- starlette/routing.py (production, modify)
- tests/test_max_body_size.py (test, create)

Tests that must pass when you finish: tests/test_max_body_size.py

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
