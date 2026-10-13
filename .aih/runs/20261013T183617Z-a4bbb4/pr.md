## Add configurable limits for the total HTTP request body size to Starlette, as upstream did in

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

- `INTENT1` Add starlette/middleware/body_limit.py with a pure ASGI RequestBodyLimitMiddleware(app, max_body_size: int) that wraps receive, counts the body bytes actually received (including multipart and encoding overhead), and answers with a plain-text 413 Content Too Large once the total exceeds the limit. (change)
- `INTENT2` Use Content-Length only as a fail-fast check: a declared length over the limit is rejected before the body is read (even if the endpoint never reads it), while an absent or understated header never bypasses the counted-bytes limit; a request exactly at the limit is allowed. (change)
- `INTENT3` When the limit is crossed after the app has started its response, send no second response; receive still raises starlette.exceptions.HTTPException(413) so standard exception handling produces the 413 or re-raises it if the response already started. (change)
- `INTENT4` Export the public constant MAX_BODY_SIZE_SCOPE_KEY = "starlette.max_body_size" from the module, store the effective limit in the ASGI scope under it while a limited app runs, and restore the previous value (or its absence) afterwards. (already_satisfied)
- `INTENT5` Add a max_body_size: int | None = None keyword to Starlette, Router, Mount and Route, where None keeps unlimited behaviour. (change)
- `INTENT6` Apply the application-level limit via the middleware stack before routing (so application middleware reading the body is limited), and let an inner route, mount or router limit override the outer one after routing, both raising and lowering it. (change)
- `INTENT7` In starlette/formparsers.py, close any temporary files created during multipart parsing whenever the parse is interrupted for any reason, including a body-limit error raised mid-stream. (already_satisfied)
- `INTENT8` Add tests for the middleware (with/without Content-Length, understated Content-Length, exactly-at-limit, multipart, streaming, no double response), for max_body_size on Starlette, Router, Mount and Route including inner limits raising and lowering the outer one, and for multipart temp-file cleanup; the existing suite must stay green. (change)

### Criteria, tests and results

| Criterion | Kind | Task | Test(s) | Result |
|---|---|---|---|---|
| ACC1 | behavior | T1 | `tests/middleware/test_body_limit.py::test_rejects_body_over_limit_without_content_length` (covered) | committed |
| ACC2 | behavior | T1 | `tests/middleware/test_body_limit.py::test_multipart_upload_counts_overhead` (covered) | committed |
| ACC3 | behavior | T1 | `tests/middleware/test_body_limit.py::test_content_length_failfast` (covered) | committed |
| ACC4 | behavior | T1 | `tests/middleware/test_body_limit.py::test_understated_content_length_and_exact_limit` (covered) | committed |
| ACC5 | behavior | T1 | `tests/middleware/test_body_limit.py::test_no_double_response` (covered) | committed |
| ACC6 | behavior | T1 | `tests/middleware/test_body_limit.py::test_scope_key_set_and_restored` (covered) | committed |
| ACC7 | behavior | T2 | tests/test_applications.py, tests/test_routing.py | committed |
| ACC8 | behavior | T1 | tests/middleware/test_body_limit.py | committed |

### Tasks

- `T1` Rework RequestBodyLimitMiddleware so the receive wrapper and Content-Length check use the effective limit from scope[MAX_BODY_SIZE_SCOPE_KEY] (falling back to self.max_body_size) at receive time, letting an inner limit raise or lower the outer one. Because an outer middleware cannot know inner overrides at call time, defer the Content-Length rejection: check the declared length against the effective scope limit at first receive and at the first http.response.start (swallowing the app's response and sending 413 so non-reading endpoints still get 413); keep no-double-response and scope restore. Add middleware tests incl. nested raise/lower. (1 attempt(s), commit `7d9aaa9fb2`)
- `T2` Add max_body_size: int | None = None to Starlette (applied in the middleware stack before routing so app middleware is limited), Router, Mount and Route (wrapping their app with RequestBodyLimitMiddleware when not None), with tests including inner raise/lower. (1 attempt(s), commit `b03f8a8ee0`)

### Gate failures along the way

- change.implemented (T3, attempt 1): change.replan_requested

### Cost (client-side estimate)

- sessions: 9; turns: 52; USD: 0.94; attempts: 3

_Generated by aih run `20261013T183617Z-a4bbb4`._
