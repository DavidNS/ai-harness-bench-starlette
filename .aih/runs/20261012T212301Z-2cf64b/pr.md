## Add configurable limits for the total HTTP request body size to Starlette, as upstream did in

### Plan

Add a pure ASGI RequestBodyLimitMiddleware, make multipart temp-file cleanup cover every interruption, then wire a max_body_size keyword into Starlette, Router, Mount and Route. An inner limit overrides the outer one by unwrapping the outer limiter's receive wrapper. The middleware and the formparsers fix are independent; the wiring depends on the middleware.

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

- `INTENT1` Add starlette/middleware/body_limit.py with a pure ASGI RequestBodyLimitMiddleware(app, max_body_size: int) that wraps receive, counts the body bytes actually received (including multipart file data and encoding overhead), and answers with a plain-text 413 Content Too Large when the total exceeds max_body_size. (change)
- `INTENT2` Use Content-Length only as a fail-fast check: a declared length above the limit is rejected before the body is read, while an absent or understated header never bypasses the byte-count limit, and a body exactly at the limit is allowed. (change)
- `INTENT3` If the application has already started sending a response when the limit is crossed, the middleware sends no second response but the receive stream is still stopped by the body-limit error. (change)
- `INTENT4` Add a keyword max_body_size: int | None = None to Starlette, Router, Mount and Route, where None keeps today's unlimited behaviour. (change)
- `INTENT5` Apply the application-level limit through the middleware stack before routing (so application middleware that reads the body is subject to it), and let a limit on an inner route, mount or router override the outer one after routing, both raising and lowering it. (change)
- `INTENT6` In starlette/formparsers.py, close any temporary files the multipart parser created whenever a parse is interrupted for any reason, including a body-limit error raised mid-stream. (change)
- `INTENT7` Export nothing new at the package root; the middleware is imported only from starlette.middleware.body_limit. (change)
- `INTENT8` Add tests for the middleware (limit with and without Content-Length, understated Content-Length, exactly-at-limit, multipart uploads, streaming bodies, no double response), for max_body_size on Starlette, Router, Mount and Route including inner limits raising and lowering the outer one, and for multipart temp-file cleanup, keeping the existing suite green. (change)

### Criteria, tests and results

| Criterion | Kind | Task | Test(s) | Result |
|---|---|---|---|---|
| ACC1 | behavior | T1 | tests/middleware/test_body_limit.py | committed |
| ACC2 | behavior | T1 | tests/middleware/test_body_limit.py | committed |
| ACC3 | behavior | T1 | tests/middleware/test_body_limit.py | committed |
| ACC4 | behavior | T1 | tests/middleware/test_body_limit.py | committed |
| ACC5 | behavior | T1 | tests/middleware/test_body_limit.py | committed |
| ACC6 | behavior | T1 | tests/middleware/test_body_limit.py | committed |
| ACC7 | behavior | T1 | tests/middleware/test_body_limit.py | committed |
| ACC8 | other | T1 | tests/middleware/test_body_limit.py | committed |
| ACC9 | behavior | T3 | tests/test_max_body_size.py | committed |
| ACC10 | behavior | T3 | tests/test_max_body_size.py | committed |
| ACC11 | behavior | T3 | tests/test_max_body_size.py | committed |
| ACC12 | behavior | T3 | tests/test_max_body_size.py | committed |
| ACC13 | behavior | T3 | tests/test_max_body_size.py | committed |
| ACC14 | behavior | T2 | tests/test_formparsers.py | committed |
| ACC15 | behavior | T1 | tests/middleware/test_body_limit.py | committed |

### Tasks

- `T1` Create RequestBodyLimitMiddleware in starlette/middleware/body_limit.py with tests. It wraps receive and counts bytes, uses Content-Length only as a fail-fast check, and sends a plain-text 413 only if the response has not started. The receive wrapper raises a body-limit error that stops the stream. An inner instance must replace an outer limiter, for example by storing the original receive in a private scope key and unwrapping it, so inner limits can raise or lower the outer one. (1 attempt(s), commit `80402a1a71`)
  - reviewer note [pass 2] `starlette/middleware/body_limit.py:35`: Headers decode as latin-1, so a Content-Length like b'\xb2' makes str.isdigit() true but int() raises ValueError, which gives a 500; use a try/except around int() or an ASCII-only check, and add a test for it.
  - reviewer note [pass 4] `starlette/middleware/body_limit.py:41`: The aliases `original_receive = receive` and `max_body_size = self.max_body_size` are redundant, since the closures could use `receive` and `self.max_body_size` directly.
- `T2` In MultiPartParser.parse, close all tracked temporary files on any interruption (for example except BaseException or a try/finally on failure) and re-raise. Add a test with a stream that raises a custom exception mid-file. (1 attempt(s), commit `87f58c2d6a`)
  - reviewer note [pass 2] `tests/test_formparsers.py:636`: The new test only has one file part, so it doesn't show that every tracked file is closed when several have been created before the interruption, such as an earlier completed file plus a partial one.
- `T3` Add the max_body_size: int | None = None keyword to Starlette, Router, Mount and Route. Starlette inserts the middleware after ServerErrorMiddleware and before user middleware. Router wraps its middleware_stack core, Mount wraps its app, and Route wraps its app, each only when the value is not None. Add tests for scoping, inner raise and lower, and the default. (1 attempt(s), commit `2057ceda4f`)
  - reviewer note [pass 1] `starlette/applications.py:30`: Hiding Content-Length from the app-level limiter means Starlette(max_body_size=N) never fails fast on a declared oversized length. It only rejects after counting bytes, which departs from the fail-fast intent of requirement 1 for the app-level limit.
  - reviewer note [pass 2] `tests/test_max_body_size.py:0`: No test checks that the app-level limit still rejects an oversized declared Content-Length when no inner limit exists. No test checks that user middleware sees the restored Content-Length header. No test covers nested Mounts with different limits, or a Route limit combined with Route middleware.
  - reviewer note [pass 4] `starlette/applications.py:19`: The _HideContentLength and _RestoreContentLength scope-rewriting pair adds two extra middlewares and a private scope key. It could be replaced with a flag on the limiter or a simpler mechanism.

### Gate failures along the way

- review.approved (T3, attempt 1): review.changes_requested

### Cost (client-side estimate)

- sessions: 14; turns: 97; USD: 2.70; attempts: 3

_Generated by aih run `20261012T212301Z-2cf64b`._
