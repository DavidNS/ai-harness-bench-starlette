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
- The same behaviour as a standalone ASGI middleware, `RequestBodyLimitMiddleware`, usable with any
  ASGI application.
- The limit counts the body bytes actually received from the ASGI server, multipart file data and
  encoding overhead included. `Content-Length` is only a fail-fast check: a declared length over the
  limit is rejected before the body is read, and an absent or understated header does not let a
  body through. A request that exceeds the limit gets a plain-text `413 Content Too Large`
  response; if the application has already started its response, no second response is sent and
  the body stream is stopped.
- Multipart parsing closes its temporary files whenever a parse is interrupted, including when the
  request crosses the body limit.
- Document the option and the middleware.

Add tests for the limit on the application, router, mount and route (including an inner limit
raising and lowering the outer one), the standalone middleware, the Content-Length fail-fast, the
multipart case and the temp-file cleanup. The existing suite must stay green. Do not change
unrelated behaviour.

## Plan summary

One PR: new RequestBodyLimitMiddleware plus max_body_size wiring on Starlette/Router/Mount/Route, multipart temp-file cleanup fix, docs and tests. The app-level middleware sits just inside ServerErrorMiddleware so the 413 isn't turned into a 500.

## Your task: T1 (T1)

Add RequestBodyLimitMiddleware first, then wire max_body_size into Starlette (just inside ServerErrorMiddleware, outside user middleware), Router, Mount and Route; make MultiPartParser close temp files on any BaseException; then add tests and docs.

Acceptance criteria this task must satisfy:
- ACC1 [behavior]: With max_body_size unset (None) on Starlette, Router, Mount and Route, a large body is read in full and returns 200 as today.
- ACC2 [behavior]: Starlette, Router, Mount and Route with max_body_size=N each return 413 for a body over N and succeed for a body at or under N.
- ACC3 [behavior]: An inner route, mount or router limit higher than the app-wide limit allows a body the outer limit would reject; an inner lower limit rejects a body the outer would allow.
- ACC4 [behavior]: RequestBodyLimitMiddleware(app, max_body_size=N) wraps a plain ASGI app, returns 413 for oversized bodies and passes smaller ones.
- ACC5 [behavior]: A chunked body with no Content-Length, or an understated one, exceeding the limit yields 413; multipart bodies are counted in total including encoding overhead.
- ACC6 [behavior]: A declared Content-Length over the limit gets 413 and the app's receive is never called.
- ACC7 [behavior]: The 413 response is text/plain with body 'Content Too Large'; if the app already sent http.response.start, no second response is sent and the body stream stops.
- ACC8 [behavior]: When multipart parsing is interrupted by any exception (including the body-limit one), all spooled temp files are closed.
- ACC9 [other]: docs/applications.md, docs/routing.md and docs/middleware.md document max_body_size and RequestBodyLimitMiddleware.

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

Tests that must pass when you finish: tests/middleware/test_body_limit.py, tests/test_body_limit.py

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
