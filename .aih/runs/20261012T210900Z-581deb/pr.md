## Add configurable limits for the total HTTP request body size to Starlette, as upstream did in

> **Run ABORTED.** This branch holds what was committed before the run stopped, plus the run's artifacts. It is evidence, not a finished change.
>
> `run.aborted`: tests.red_is_error: tests/middleware/test_body_limit.py does not run on the base (import/collection error), so it proves nothing:

### Plan

Add a pure ASGI RequestBodyLimitMiddleware that keeps shared limit state in the scope so inner Route/Mount/Router limits can override the outer one. Make the multipart parser close temp files on any interruption. Then wire max_body_size into Router/Mount/Route and into Starlette's middleware stack. Four tasks; docs are not added.

### Request

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

### Intents

- `INTENT1` Add starlette/middleware/body_limit.py with a pure ASGI RequestBodyLimitMiddleware(app, max_body_size: int) that wraps receive, counts the body bytes actually received (including multipart data and encoding overhead), and answers with a plain-text 413 Content Too Large when the total exceeds max_body_size. (change)
- `INTENT2` Use Content-Length only as a fail-fast check (declared length over the limit is rejected before the body is read), while absent or understated headers never bypass the received-byte count, and a body exactly at the limit is allowed. (change)
- `INTENT3` If the app has already started a response when the limit is crossed, the middleware sends no second response but the limit still stops the receive stream (raises/terminates). (change)
- `INTENT4` Add a max_body_size: int | None = None keyword to Starlette, Router, Mount and Route, where None keeps today's unlimited behaviour. (change)
- `INTENT5` Apply the Starlette-level limit through the middleware stack before routing (so application middleware reading the body is limited), and let an inner Route/Mount/Router limit override the outer one after routing, both raising and lowering it. (change)
- `INTENT6` In starlette/formparsers.py, close any temporary files the multipart parser created whenever parsing is interrupted for any reason, including a body-limit error raised mid-stream. (change)
- `INTENT7` Add tests covering the middleware (with/without Content-Length, understated Content-Length, exactly-at-limit, multipart, streaming bodies, no double response), max_body_size on Starlette/Router/Mount/Route including inner raising and lowering the outer limit, and multipart temp-file cleanup. (change)

### Criteria, tests and results

| Criterion | Kind | Task | Test(s) | Result |
|---|---|---|---|---|
| ACC1 | behavior | T1 | tests/middleware/test_body_limit.py | pending |
| ACC2 | behavior | T1 | tests/middleware/test_body_limit.py | pending |
| ACC3 | behavior | T1 | tests/middleware/test_body_limit.py | pending |
| ACC4 | behavior | T3, T4 | tests/test_applications.py, tests/test_routing.py | pending |
| ACC5 | behavior | T3, T4 | tests/test_applications.py, tests/test_routing.py | pending |
| ACC6 | behavior | T2 | tests/test_formparsers.py | pending |
| ACC7 | other | T1 | tests/middleware/test_body_limit.py | pending |

### Tasks

- `T1` Create RequestBodyLimitMiddleware. It stores shared state in scope (limit, received count, response-started flag), so a nested instance overrides the limit instead of double counting. It does the Content-Length fail-fast check, counts received bytes, sends a plain-text 413 only if no response has started, and always raises or stops the receive stream. (2 attempt(s), commit ``)
- `T2` In MultiPartParser.parse, close the tracked temp files on any exception (use except BaseException or a finally flag) and re-raise. Add tests with a stream that raises mid-upload. (0 attempt(s), commit ``)
- `T3` Add keyword max_body_size: int | None = None to Route, Mount and Router. When not None, wrap the app (or the Router middleware stack) in RequestBodyLimitMiddleware so it overrides any outer limit. (0 attempt(s), commit ``)
- `T4` Add max_body_size to Starlette. Insert the middleware in build_middleware_stack after ServerErrorMiddleware and before user middleware. Pass the limit so the Starlette-level limit is the one in scope before routing. Test an inner Route, Mount or Router raising or lowering the limit, and user middleware reading the body. (0 attempt(s), commit ``)

### Gate failures along the way

- tests.red_proven (T1, attempt 1): tests.red_is_error
- tests.red_proven (T1, attempt 2): tests.red_is_error

### Cost (client-side estimate)

- sessions: 5; turns: 42; USD: 0.76; attempts: 2

_Generated by aih run `20261012T210900Z-581deb`._
