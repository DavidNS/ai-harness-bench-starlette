## Add configurable limits for the total HTTP request body size.

### Plan

Single PR: a shared per-request body-limit state and middleware in starlette/middleware/body_limit.py, wired through Starlette/Router/Mount/Route with Content-Length checks deferred until routing settles, multipart temp-file cleanup hardened, plus docs and tests.

### Request

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

### Intents

- `INTENT1` Add a `max_body_size` keyword option (default `None`, meaning unlimited as today) to `Starlette`, `Router`, `Mount` and `Route`. (change)
- `INTENT2` The application-level limit applies before routing, and a limit on an inner route, mount or router overrides the outer one for that part of the tree, whether it raises or lowers the outer limit. (change)
- `INTENT3` Provide `RequestBodyLimitMiddleware(app, max_body_size=...)` in `starlette.middleware.body_limit`, working with any ASGI application and not only Starlette. (change)
- `INTENT4` Enforce the limit on the body bytes actually received from the ASGI server (multipart file data and encoding overhead included), so an absent or understated Content-Length does not let an oversized body through. (change)
- `INTENT5` Reject a declared Content-Length over the limit before any application code runs: in Starlette, once routing has settled the applicable limit (matched route's own, else the outer one, else the app's for no-route or no-method matches); in the standalone middleware, on entry. (change)
- `INTENT6` A request exceeding the limit gets a 413 response with the plain-text body `Content Too Large`; if the application has already started its response, no second response is sent and the body stream is stopped. (change)
- `INTENT7` Multipart parsing closes its temporary files whenever a parse is interrupted, including when the body limit is crossed. (change)
- `INTENT8` Requests without a body, websockets and lifespan are unaffected by the limit. (change)
- `INTENT9` Document the `max_body_size` option and `RequestBodyLimitMiddleware` in the project docs. (change)
- `INTENT10` Add tests covering the limit on app, router, mount and route (including an inner limit raising and lowering the outer one), the standalone middleware, Content-Length fail-fast, the multipart case and temp-file cleanup. (change)

### Criteria, tests and results

| Criterion | Kind | Task | Test(s) | Result |
|---|---|---|---|---|
| ACC1 | behavior | T1 | `tests/benchjudge/test_body_limit.py::test_default_is_unlimited` (covered, probe verified / base fails) | committed |
| ACC2 | behavior | T1 | `tests/benchjudge/test_body_limit.py::test_mount_router_limits` (covered, probe verified / base fails) | committed |
| ACC3 | behavior | T1 | `tests/benchjudge/test_body_limit.py::test_middleware_plain_asgi_app` (covered, probe verified / base fails) | committed |
| ACC4 | behavior | T1 | `tests/benchjudge/test_body_limit.py::test_app_limit_without_content_length_or_understated` (covered, probe verified / base fails) | committed |
| ACC5 | behavior | T1 | `tests/benchjudge/test_body_limit.py::test_content_length_fail_fast_no_route_and_no_method` (covered, probe verified / base fails) | committed |
| ACC6 | behavior | T1 | `tests/benchjudge/test_body_limit.py::test_response_started_then_limit_crossed_disconnects` (covered, probe verified / base fails) | committed |
| ACC7 | behavior | T1 | `tests/benchjudge/test_body_limit.py::test_multipart_files_closed_on_body_limit` (covered, probe verified / base fails) | committed |
| ACC8 | behavior | T1 | `tests/benchjudge/test_body_limit.py::test_websocket_and_lifespan_unaffected` (covered, probe verified / base fails) | committed |
| ACC9 | other | T1 | tests/benchjudge/test_body_limit.py | committed |
| ACC10 | behavior | T1 | `tests/benchjudge/test_body_limit.py::test_route_raises_app_limit` (covered, probe verified / base fails) | committed |

### Tasks

- `T1` Add starlette/middleware/body_limit.py with a per-request limit state (counts received http.request bytes, mutable limit, tracks response started, sends 413 'Content Too Large' once or ends the stream with http.disconnect if the response started) plus RequestBodyLimitMiddleware (http only, Content-Length checked on entry). Then thread max_body_size through Router.app, Route.handle, Mount.handle and Starlette; Content-Length is checked only after the applicable limit settles (matched route/mount/router, partial 405 match, or app limit before redirect/default/404). Harden MultiPartParser.parse to close files on any exception. Then add tests and docs. (1 attempt(s), commit `7ebdf72058`)
  - reviewer note [pass 2] `tests/benchjudge/test_body_limit.py:0`: No test combines a declared Content-Length with an inner Mount/Router limit that raises or lowers the app limit (only route-level fail-fast is tested).
  - reviewer note [pass 2] `tests/benchjudge/test_body_limit.py:340`: test_response_started_in_starlette_streaming asserts only `status in (200, 413)`, so it is a weak check of the already-started-response path inside Starlette.
  - reviewer note [pass 4] `starlette/routing.py:456`: Mount.handle's getattr-based router sniffing to choose check_content_length is convoluted; a mounted Starlette/Router could always defer the check.

### Cost (client-side estimate)

- sessions: 4; turns: 48; USD: 1.02; attempts: 1

_Generated by aih run `20261022T170634Z-5da588`._
