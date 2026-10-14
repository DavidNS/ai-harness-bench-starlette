## Add configurable limits for the total HTTP request body size (encode/starlette issue #2155).

### Plan

One PR: new RequestBodyLimitMiddleware plus max_body_size wiring on Starlette/Router/Mount/Route, multipart temp-file cleanup fix, docs and tests. The app-level middleware sits just inside ServerErrorMiddleware so the 413 isn't turned into a 500.

### Request

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

### Intents

- `INTENT1` Add a `max_body_size` option (default `None`, meaning unlimited as today) to `Starlette`, `Router`, `Mount` and `Route`. (change)
- `INTENT2` The application-level limit applies before routing, and a limit set on an inner route, mount or router overrides the outer one for that part of the tree, whether it raises or lowers the outer limit. (change)
- `INTENT3` Provide a standalone ASGI middleware `RequestBodyLimitMiddleware` with the same limit behaviour that works with any ASGI application. (change)
- `INTENT4` Count the body bytes actually received from the ASGI server (including multipart file data and encoding overhead) against the limit, so an absent or understated `Content-Length` does not let an oversized body through. (change)
- `INTENT5` Reject a request whose declared `Content-Length` exceeds the limit with a 413 before any of the body is read. (change)
- `INTENT6` A request that exceeds the limit gets a plain-text `413 Content Too Large` response; if the application has already started its response, no second response is sent and the body stream is stopped. (change)
- `INTENT7` Multipart parsing closes its temporary files whenever a parse is interrupted, including when the request crosses the body limit. (change)
- `INTENT8` Document the `max_body_size` option and `RequestBodyLimitMiddleware`. (change)
- `INTENT9` Add tests for the limit on the application, router, mount and route (including an inner limit raising and lowering the outer one), the standalone middleware, the Content-Length fail-fast, the multipart case and temp-file cleanup. (change)

### Criteria, tests and results

| Criterion | Kind | Task | Test(s) | Result |
|---|---|---|---|---|
| ACC1 | behavior | T1 | `tests/test_body_limit.py::test_unlimited_by_default` (covered) | committed |
| ACC2 | behavior | T1 | `tests/test_body_limit.py::test_application_limit` (covered) | committed |
| ACC3 | behavior | T1 | `tests/test_body_limit.py::test_inner_limit_raises_outer` (covered) | committed |
| ACC4 | behavior | T1 | `tests/middleware/test_body_limit.py::test_over_limit_returns_413` (covered) | committed |
| ACC5 | behavior | T1 | `tests/test_body_limit.py::test_multipart_counts_total_including_encoding_overhead` (covered) | committed |
| ACC6 | behavior | T1 | `tests/middleware/test_body_limit.py::test_declared_content_length_fails_fast_without_receive` (covered) | committed |
| ACC7 | behavior | T1 | `tests/middleware/test_body_limit.py::test_response_already_started_sends_no_second_response` (covered) | committed |
| ACC8 | behavior | T1 | `tests/test_body_limit.py::test_multipart_closes_temp_files_on_any_exception` (covered) | committed |
| ACC9 | other | T1 | tests/middleware/test_body_limit.py, tests/test_body_limit.py | committed |

### Tasks

- `T1` Add RequestBodyLimitMiddleware first, then wire max_body_size into Starlette (just inside ServerErrorMiddleware, outside user middleware), Router, Mount and Route; make MultiPartParser close temp files on any BaseException; then add tests and docs. (1 attempt(s), commit `b4143f64ad`)
  - reviewer note [pass 2] `tests/test_body_limit.py:0`: No test sends an oversized body through a BaseHTTPMiddleware that calls call_next without reading the body first, so exception propagation through its task group is not exercised.
  - reviewer note [pass 4] `tests/test_body_limit.py:126`: test_application_router_limit_overrides_outer sets router.max_body_size by hand and only asserts 413, which does not prove an override; drop it or replace it with a real Router-level test.
  - reviewer note [pass 4] `tests/middleware/test_body_limit.py:96`: test_declared_content_length_checked_lazily_when_eager_check_disabled asserts that receive is never reached, the same as the eager test, so it does not show the lazy path differs.

### Gate failures along the way

- plan (plan, attempt 1): plan.evidence_quote_not_found

### Cost (client-side estimate)

- sessions: 5; turns: 60; USD: 1.01; attempts: 1

_Generated by aih run `20261014T151105Z-9cbacb`._
