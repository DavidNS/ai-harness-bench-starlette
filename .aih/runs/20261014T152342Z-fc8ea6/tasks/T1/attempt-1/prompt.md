You are implementing one task of an approved plan inside an isolated worktree. You can read and
edit files. The only command you can run is `aih check <test files...>`, which runs those tests
(or `aih check --build`, which only compiles, when the project has a build step): use it as often
as you need; every other shell command is rejected. When you try to finish, the harness runs the
task's tests again and gives you the result. Edits outside the files listed for this task are
rejected by a hook; do not work around it.

## Original request

Add configurable limits for the total HTTP request body size (encode/starlette issue #2155).

Today any Starlette application can be taken down by a client that streams a huge body: nothing
stops `await request.json()` from buffering gigabytes. We need a limit the application can set,
with a 413 response when a request exceeds it.

Requirements:

- A `max_body_size` option on `Starlette`, `Router`, `Mount` and `Route`. Default `None` keeps
  today's unlimited behaviour. The application-level limit applies before routing; a limit set on
  an inner route, mount or router overrides the outer one for that part of the tree, so it can
  raise or lower the application-wide limit.
- The same behaviour as a standalone ASGI middleware, `RequestBodyLimitMiddleware`, importable from
  `starlette.middleware.body_limit` and usable with any ASGI application.
- The limit counts the body bytes actually received from the ASGI server, multipart file data and
  encoding overhead included. `Content-Length` is only a fail-fast check: a declared length over the
  limit is rejected before the body is read, and an absent or understated header does not let a
  body through. A request that exceeds the limit gets a 413 response with the plain-text body
  `Content Too Large`; if the application has already started its response, no second response is
  sent and the body stream is stopped.
- Multipart parsing closes its temporary files whenever a parse is interrupted, including when the
  request crosses the body limit.
- Document the option and the middleware.

Add tests for the limit on the application, router, mount and route (including an inner limit
raising and lowering the outer one), the standalone middleware, the Content-Length fail-fast, the
multipart case and the temp-file cleanup. The existing suite must stay green. Do not change
unrelated behaviour.

## Plan summary

Single PR: new RequestBodyLimitMiddleware plus max_body_size on Starlette/Router/Mount/Route, multipart temp-file cleanup on any interruption, tests and docs. Docs are required by INTENT7.

## Your task: T1 (T1)

Order: (1) add starlette/middleware/body_limit.py: middleware wraps receive/send once per request, counts bytes, checks Content-Length up-front, on overflow sends 413 'Content Too Large' via PlainTextResponse if not started, then returns http.disconnect from receive and drops later sends; the wrapper state is kept in scope so an inner middleware instance only changes the effective limit (raise or lower) instead of re-wrapping. (2) Add max_body_size to Router, Mount, Route (wrap their app via the middleware only when not None) and Starlette (put the middleware outermost, outside ServerErrorMiddleware). (3) In formparsers close files on any exception (BaseException) during parse. (4) Tests, then docs.

Acceptance criteria this task must satisfy:
- ACC1 [behavior]: RequestBodyLimitMiddleware(app, max_body_size=N) wraps any ASGI app; with None or no limit all bodies pass; bytes received from the server (incl. chunked, no Content-Length) are counted.
- ACC2 [behavior]: A body exceeding the limit yields 413 with plain-text body 'Content Too Large'; if the app already started its response, no second response is sent and receive() returns http.disconnect.
- ACC3 [behavior]: Declared Content-Length over limit gives 413 without the app reading the body; missing or understated Content-Length is still enforced on actual bytes.
- ACC4 [behavior]: Starlette, Router, Mount and Route accept max_body_size (default None = unlimited) and enforce it.
- ACC5 [behavior]: App-level limit applies before routing; inner router/mount/route limits override the outer limit both raising and lowering it.
- ACC6 [behavior]: Multipart bodies (file data and encoding overhead) count toward the limit, and multipart temp files are closed on any interrupted parse including crossing the limit.
- ACC7 [other]: docs/middleware.md, docs/applications.md and docs/routing.md document max_body_size and RequestBodyLimitMiddleware.

Files you may edit (anything else is blocked):
- starlette/middleware/body_limit.py (production, create)
- starlette/applications.py (production, modify)
- starlette/routing.py (production, modify)
- starlette/formparsers.py (production, modify)
- docs/middleware.md (production, modify)
- docs/applications.md (production, modify)
- docs/routing.md (production, modify)
- tests/middleware/test_body_limit.py (test, create)
- tests/test_body_limit.py (test, create)
- tests/test_formparsers.py (test, modify)

Tests that must pass when you finish: tests/middleware/test_body_limit.py, tests/test_body_limit.py, tests/test_formparsers.py

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
