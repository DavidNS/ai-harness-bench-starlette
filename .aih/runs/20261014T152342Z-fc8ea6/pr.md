## Add configurable limits for the total HTTP request body size (encode/starlette issue #2155).

### Plan

Single PR: new RequestBodyLimitMiddleware plus max_body_size on Starlette/Router/Mount/Route, multipart temp-file cleanup on any interruption, tests and docs. Docs are required by INTENT7.

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

### Intents

- `INTENT1` Add RequestBodyLimitMiddleware in starlette/middleware/body_limit.py, usable with any ASGI app, that counts the body bytes actually received from the server and defaults to no limit. (change)
- `INTENT2` When received bytes exceed the limit, respond 413 with plain-text body `Content Too Large`; if the response has already started, send no second response and stop the body stream. (change)
- `INTENT3` Reject a declared Content-Length over the limit with 413 before reading the body, while an absent or understated Content-Length still gets enforced on the bytes actually received. (change)
- `INTENT4` Add a `max_body_size` option (default None, meaning unlimited as today) to Starlette, Router, Mount and Route. (change)
- `INTENT5` The application-level limit applies before routing, and a limit on an inner router, mount or route overrides the outer one for that part of the tree, whether it raises or lowers the outer limit. (change)
- `INTENT6` Multipart bodies count toward the limit (file data and encoding overhead included), and multipart parsing closes its temporary files whenever a parse is interrupted, including when the body limit is crossed. (change)
- `INTENT7` Document the `max_body_size` option and RequestBodyLimitMiddleware in the docs. (change)
- `INTENT8` Add tests for the limit on the app, router, mount and route (including inner limits raising and lowering the outer one), the standalone middleware, the Content-Length fail-fast, the multipart case and the temp-file cleanup. (change)

### Criteria, tests and results

| Criterion | Kind | Task | Test(s) | Result |
|---|---|---|---|---|
| ACC1 | behavior | T1 | `tests/middleware/test_body_limit.py::test_chunked_body_without_content_length_is_counted` (covered) | committed |
| ACC2 | behavior | T1 | `tests/middleware/test_body_limit.py::test_response_already_started_sends_no_second_response` (covered) | committed |
| ACC3 | behavior | T1 | `tests/middleware/test_body_limit.py::test_declared_content_length_is_rejected_before_body_is_read` (covered) | committed |
| ACC4 | behavior | T1 | `tests/test_body_limit.py::test_route_limit` (covered) | committed |
| ACC5 | behavior | T1 | `tests/test_body_limit.py::test_inner_limits_lower_outer_limit` (covered) | committed |
| ACC6 | behavior | T1 | `tests/test_body_limit.py::test_multipart_temp_files_closed_when_limit_is_crossed` (covered) | committed |
| ACC7 | other | T1 | tests/middleware/test_body_limit.py, tests/test_body_limit.py, tests/test_formparsers.py | committed |

### Tasks

- `T1` Order: (1) add starlette/middleware/body_limit.py: middleware wraps receive/send once per request, counts bytes, checks Content-Length up-front, on overflow sends 413 'Content Too Large' via PlainTextResponse if not started, then returns http.disconnect from receive and drops later sends; the wrapper state is kept in scope so an inner middleware instance only changes the effective limit (raise or lower) instead of re-wrapping. (2) Add max_body_size to Router, Mount, Route (wrap their app via the middleware only when not None) and Starlette (put the middleware outermost, outside ServerErrorMiddleware). (3) In formparsers close files on any exception (BaseException) during parse. (4) Tests, then docs. (1 attempt(s), commit `7781064829`)
  - reviewer note [pass 1] `starlette/middleware/body_limit.py:38`: The Content-Length fail-fast is lazy: it runs on the first receive() or the response start, not when the request arrives. An app that does side effects before reading or responding is not stopped early. This is a deliberate trade-off to let inner limits override the outer one, but it is not documented.
  - reviewer note [pass 2] `tests/test_body_limit.py:1`: No test combines max_body_size with user middleware that wraps receive (for example BaseHTTPMiddleware), or checks websocket scopes through Starlette(max_body_size=...). Middleware-level pass-through for a non-http scope is tested.
  - reviewer note [pass 4] `tests/middleware/test_body_limit.py:78`: collect_http returns a tuple whose second element is always None and is never used, so it can return just the sent list.

### Gate failures along the way

- review.approved (T1, attempt 1): review.changes_requested

### Cost (client-side estimate)

- sessions: 6; turns: 78; USD: 1.18; attempts: 1

_Generated by aih run `20261014T152342Z-fc8ea6`._
