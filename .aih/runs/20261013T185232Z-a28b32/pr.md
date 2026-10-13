## Add configurable limits for the total HTTP request body size to Starlette, as upstream did in

### Plan

T1 added the middleware and multipart cleanup, but the Starlette/Router/Mount/Route wiring is not in the worktree, despite the earlier note. T2 changes the middleware so the Content-Length fail-fast is deferred until the effective limit is final: inner limits can raise the limit, and endpoints that never read the body are still rejected. T3 adds the `max_body_size` wiring and tests/test_max_body_size.py.

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

### Intents

- `INTENT1` Add starlette/middleware/body_limit.py with pure ASGI RequestBodyLimitMiddleware(app, max_body_size: int) that wraps receive, counts actual body bytes received (including multipart and encoding overhead), and answers with a plain-text 413 Content Too Large when the total exceeds the limit. (already_satisfied)
- `INTENT2` Use Content-Length only as a fail-fast check: a declared length over the limit is rejected before the body is read (even if the endpoint never reads it), while an absent or understated header never bypasses the byte-count limit, and exactly-at-limit bodies are allowed. (change)
- `INTENT3` When the limit is crossed after the app has started its response, the middleware sends no second response and receive raises starlette.exceptions.HTTPException(413) so standard exception handling produces the 413 (or re-raises if the response has already started). (already_satisfied)
- `INTENT4` Export the public constant MAX_BODY_SIZE_SCOPE_KEY = "starlette.max_body_size" from the module; the effective limit is stored in the scope under it while a limited app runs and the previous value (or its absence) is restored afterwards. (already_satisfied)
- `INTENT5` Add a max_body_size: int | None = None keyword to Starlette, Router, Mount and Route, where None keeps unlimited behaviour. (change)
- `INTENT6` Apply the Starlette application-level limit through the middleware stack before routing, so application middleware that reads the body is subject to it. (change)
- `INTENT7` A limit on an inner Route, Mount or Router overrides the outer limit after routing, allowing it to be either larger or smaller than the outer one. (change)
- `INTENT8` In starlette/formparsers.py, close any temporary files created during multipart parsing when the parse is interrupted for any reason, including a body-limit error raised mid-stream. (already_satisfied)
- `INTENT9` Add tests covering the middleware (with/without Content-Length, understated Content-Length, exactly-at-limit, multipart uploads, streaming bodies, no double response), max_body_size on Starlette/Router/Mount/Route including inner limits raising and lowering the outer one, and multipart temp-file cleanup, with the existing suite staying green. (change)

### Criteria, tests and results

| Criterion | Kind | Task | Test(s) | Result |
|---|---|---|---|---|
| ACC1 | behavior | T1 | `tests/middleware/test_body_limit.py::test_limit_enforced_without_content_length` (covered) | committed |
| ACC2 | behavior | T1 | `tests/middleware/test_body_limit.py::test_content_length_failfast` (covered) | committed |
| ACC3 | behavior | T1 | `tests/middleware/test_body_limit.py::test_understated_content_length_and_exact_limit` (covered) | committed |
| ACC4 | behavior | T1 | `tests/middleware/test_body_limit.py::test_multipart_and_streaming_counted` (covered) | committed |
| ACC5 | behavior | T1 | `tests/middleware/test_body_limit.py::test_no_double_response` (covered) | committed |
| ACC6 | behavior | T1 | `tests/middleware/test_body_limit.py::test_scope_key` (covered) | committed |
| ACC7 | behavior | T1 | `tests/test_formparsers.py::test_multipart_temp_files_closed_on_interrupt` (covered) | committed |
| ACC11 | other | T1 | tests/middleware/test_body_limit.py, tests/test_formparsers.py | committed |
| ACC12 | behavior | T3 | `tests/middleware/test_body_limit.py::test_nested_limits_raise_and_lower_the_outer_limit` (covered) | committed |
| ACC13 | behavior | T3 | `tests/middleware/test_body_limit.py::test_content_length_failfast_sends_exactly_one_response` (covered) | committed |
| ACC14 | behavior | T3 | `tests/middleware/test_body_limit.py::test_understated_content_length_and_exact_limit` (covered) | committed |
| ACC15 | behavior | T3 | `tests/middleware/test_body_limit.py::test_no_double_response` (covered) | committed |
| ACC16 | behavior | T3 | `tests/middleware/test_body_limit.py::test_scope_key` (covered) | committed |
| ACC17 | behavior | T4 | `tests/test_max_body_size.py::test_limit_with_content_length` (covered) | committed |
| ACC18 | behavior | T4 | `tests/test_max_body_size.py::test_app_middleware_reading_body_is_limited` (covered) | committed |
| ACC19 | behavior | T4 | `tests/test_max_body_size.py::test_inner_limit_raises_outer_limit` (covered) | committed |
| ACC20 | behavior | T4 | `tests/test_max_body_size.py::test_multipart_temp_files_closed_on_body_limit` (covered) | committed |
| ACC21 | other | T4 | tests/test_max_body_size.py | committed |

### Tasks

- `T1` Create RequestBodyLimitMiddleware with MAX_BODY_SIZE_SCOPE_KEY, a shared counting receive wrapper that reads the effective limit from scope dynamically (so nested limited apps just swap the scope value), Content-Length fail-fast, and no-double-response handling. Make MultiPartParser.parse close its temp files on any BaseException. Add tests. (1 attempt(s), commit `e3b6bba4a4`)
  - reviewer note [pass 2] `starlette/middleware/body_limit.py:47`: `str.isdigit()` is true for characters like '²' that `int()` rejects, so a crafted Content-Length header raises ValueError (a 500); no test covers it, and `isascii() and isdigit()` would avoid it.
  - reviewer note [pass 2] `tests/middleware/test_body_limit.py:0`: No test checks that the scope key and state key are restored or removed when the app raises (the `finally` path).
  - reviewer note [pass 4] `starlette/middleware/body_limit.py:88`: `_reject` is a one-line wrapper used twice and could be inlined.
- `T3` Change RequestBodyLimitMiddleware so the outermost instance defers the Content-Length fail-fast until the effective limit is final. It tracks the innermost limit in the shared state. On the app's first http.response.start, or when the app returns without a response, it replaces the app's response with a 413 if the declared length exceeds that limit. Nested instances keep their own immediate check and update the tracked limit. Add tests. (1 attempt(s), commit `42739b5c37`)
  - reviewer note [pass 2] `tests/middleware/test_body_limit.py:240`: No test sends a Content-Length above an inner lowered limit but below the outer one; the nested immediate-check branch (body_limit.py:53) is untested with a declared length.
  - reviewer note [pass 2] `starlette/middleware/body_limit.py:82`: If the app raises a non-HTTPException error with an oversized Content-Length and no response, the 413 is skipped and the error propagates; no test covers this.
  - reviewer note [pass 3] `tests/middleware/test_body_limit.py:86`: The assertion `never_reads.called is False` became `is True`; this follows from the deferred design, and the new exactly-one-response test compensates.
  - reviewer note [pass 4] `starlette/middleware/body_limit.py:82`: state.limit is set on entry by each nested instance but never restored on exit, so after an inner app returns the outer check uses the inner value; harmless today but fragile.
- `T4` Add the max_body_size keyword to Starlette, Router, Mount and Route. Starlette inserts the middleware after ServerErrorMiddleware and before user middleware. Route, Mount and Router wrap their app outermost, only when the value is not None. Add tests/test_max_body_size.py for all four classes, including inner limits that raise and lower the outer one, app middleware that reads the body, and multipart temp-file cleanup under a limit. (1 attempt(s), commit `01729a6276`)
  - reviewer note [pass 2] `tests/test_max_body_size.py:0`: No test sends a lifespan or websocket scope through a limited Starlette, Router or Mount. The middleware passes non-http scopes through, but nothing here checks that the new wiring does not break them.
  - reviewer note [pass 2] `tests/test_max_body_size.py:37`: Router and Mount are only tested nested inside a Starlette app. A Router used directly as the ASGI app with max_body_size is not tested.
  - reviewer note [pass 4] `starlette/applications.py:55`: The new `self.max_body_size` public attribute is not required and is only read once, in build_middleware_stack. The `body_limit_middleware` temporary list could be folded into the stack expression.

### Gate failures along the way

- plan (plan, attempt 1): plan.evidence_quote_not_found
- change.implemented (T2, attempt 1): change.replan_requested

### Cost (client-side estimate)

- sessions: 12; turns: 115; USD: 1.80; attempts: 4

_Generated by aih run `20261013T185232Z-a28b32`._
