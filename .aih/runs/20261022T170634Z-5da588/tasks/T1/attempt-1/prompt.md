You are implementing one task of an approved plan inside an isolated worktree. You can read and
edit files and you have a shell: run the project's tests the way its developers do (keep the
output short, e.g. `| tail`), its formatter and linters, its code generators, `git diff` to review
your change. Destructive commands, pushing and the network are denied by a hook; do not work
around it. A scratch test to try a hypothesis is fine if named with `aih_probe` in the file name and deleted before you
finish (those are the only files you may create outside the task's list). When you try to finish,
the harness runs the task's tests and gives you the result; the whole suite runs once after the
last task. Edits outside the files listed for this task are rejected by a hook; if the task really
needs another file, say so in your final answer (`replan_request` with the files) and the harness
will add it to your task.

## Original request

Add configurable limits for the total HTTP request body size.

Today any Starlette application can be taken down by a client that streams a huge body: nothing
stops `await request.json()` from buffering gigabytes. We need a limit the application can set,
with a 413 response when a request exceeds it.

Requirements:

- A `max_body_size` keyword option on `Starlette`, `Router`, `Mount` and `Route`. Default `None`
  keeps today's unlimited behaviour. The application-level limit applies before routing; a limit
  set on an inner route, mount or router overrides the outer one for that part of the tree, so it
  can raise or lower the application-wide limit.
- The same behaviour as a standalone ASGI middleware, `RequestBodyLimitMiddleware(app,
  max_body_size=...)`, importable from `starlette.middleware.body_limit` and usable with any ASGI
  application, not only a Starlette one.
- The limit counts the body bytes actually received from the ASGI server, multipart file data and
  encoding overhead included. `Content-Length` is only a fail-fast check, and fail-fast means
  before any application code runs: a declared length over the limit is rejected as soon as the
  limit that applies is settled. Inside a Starlette application that is when routing has decided,
  since an inner route, mount or router may still raise the limit: the matched route's own limit,
  the outer one for a route without its own, and the application's for a request that matches no
  route or no method. The standalone middleware has nothing below it that can change its limit, so
  it rejects on entry. An absent or understated header does not let a body through. A request that
  exceeds the limit gets a 413 response with the plain-text body `Content Too Large`; if the
  application has already started its response, no second response is sent and the body stream is
  stopped.
- Multipart parsing closes its temporary files whenever a parse is interrupted, including when the
  request crosses the body limit.
- Requests without a body, websockets and lifespan are unaffected.
- Document the option and the middleware.

Add tests for the limit on the application, router, mount and route (including an inner limit
raising and lowering the outer one), the standalone middleware, the Content-Length fail-fast, the
multipart case and the temp-file cleanup. The existing suite must stay green. Do not change
unrelated behaviour.

## Plan summary

Single PR: a shared per-request body-limit state and middleware in starlette/middleware/body_limit.py, wired through Starlette/Router/Mount/Route with Content-Length checks deferred until routing settles, multipart temp-file cleanup hardened, plus docs and tests.

## Your task: T1 (T1)

Add starlette/middleware/body_limit.py with a per-request limit state (counts received http.request bytes, mutable limit, tracks response started, sends 413 'Content Too Large' once or ends the stream with http.disconnect if the response started) plus RequestBodyLimitMiddleware (http only, Content-Length checked on entry). Then thread max_body_size through Router.app, Route.handle, Mount.handle and Starlette; Content-Length is checked only after the applicable limit settles (matched route/mount/router, partial 405 match, or app limit before redirect/default/404). Harden MultiPartParser.parse to close files on any exception. Then add tests and docs.

Acceptance criteria this task must satisfy:
- ACC1 [behavior]: Starlette, Router, Mount and Route accept max_body_size; default None leaves arbitrarily large bodies accepted.
- ACC2 [behavior]: App-level limit applies before routing; a Route/Mount/Router limit higher than the outer one lets a body through that the outer limit would reject, a lower one rejects a body the outer limit would allow; routes without their own limit use the outer one.
- ACC3 [behavior]: RequestBodyLimitMiddleware wraps a plain (non-Starlette) ASGI app and returns 413 'Content Too Large' text/plain when the received body exceeds max_body_size, and passes bodies at or under the limit.
- ACC4 [behavior]: A body with absent or understated Content-Length that exceeds the limit is rejected; multipart bodies are counted in full including boundaries.
- ACC5 [behavior]: A declared Content-Length over the limit gets 413 without endpoint code running: the middleware rejects on entry; in Starlette the matched route's own limit (or outer, or app's for no-route/405) decides, so an inner route raising the limit allows the request.
- ACC6 [behavior]: If the app already started its response when the limit is crossed, no second response start is sent and the receive stream ends/disconnects.
- ACC7 [behavior]: When a multipart parse is interrupted by any exception (including body limit exceeded), all temporary files created are closed.
- ACC8 [behavior]: GET requests without a body, websocket connections and lifespan work unchanged when a limit is set.
- ACC9 [other]: Docs describe max_body_size on Starlette/Router/Mount/Route and RequestBodyLimitMiddleware.
- ACC10 [behavior]: Tests exist for app, router, mount, route (inner raise and lower), standalone middleware, Content-Length fail-fast, multipart, and temp-file cleanup.

Files you may edit (anything else is blocked):
- starlette/middleware/body_limit.py (production, create)
- starlette/routing.py (production, modify)
- starlette/applications.py (production, modify)
- starlette/formparsers.py (production, modify)
- docs/middleware.md (production, modify)
- docs/applications.md (production, modify)
- docs/routing.md (production, modify)
- tests/benchjudge/test_body_limit.py (test, create)

Tests that must pass when you finish: tests/benchjudge/test_body_limit.py

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
