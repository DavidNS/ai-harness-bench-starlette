You are planning a change in this repository. You can read files but not edit them or run
commands. Another session will implement your plan, one task at a time, in a fresh context, and a
harness will check the result against it mechanically. Explore the repository as you see fit;
only your final JSON answer is used.

## Request

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

## Intents (use these ids exactly)

- `INTENT1` (body-limit-middleware): Add starlette/middleware/body_limit.py with a pure ASGI RequestBodyLimitMiddleware(app, max_body_size: int) that wraps receive, counts the body bytes actually received (including multipart data and encoding overhead), and answers with a plain-text 413 Content Too Large when the total exceeds max_body_size.
- `INTENT2` (content-length-failfast): Use Content-Length only as a fail-fast check (declared length over the limit is rejected before the body is read), while absent or understated headers never bypass the received-byte count, and a body exactly at the limit is allowed.
- `INTENT3` (no-double-response): If the app has already started a response when the limit is crossed, the middleware sends no second response but the limit still stops the receive stream (raises/terminates).
- `INTENT4` (max-body-size-param): Add a max_body_size: int | None = None keyword to Starlette, Router, Mount and Route, where None keeps today's unlimited behaviour.
- `INTENT5` (limit-layering): Apply the Starlette-level limit through the middleware stack before routing (so application middleware reading the body is limited), and let an inner Route/Mount/Router limit override the outer one after routing, both raising and lowering it.
- `INTENT6` (multipart-cleanup): In starlette/formparsers.py, close any temporary files the multipart parser created whenever parsing is interrupted for any reason, including a body-limit error raised mid-stream.
- `INTENT7` (tests): Add tests covering the middleware (with/without Content-Length, understated Content-Length, exactly-at-limit, multipart, streaming bodies, no double response), max_body_size on Starlette/Router/Mount/Route including inner raising and lowering the outer limit, and multipart temp-file cleanup.

## Constraints

- Export nothing new at the package root; import the middleware from starlette.middleware.body_limit
- Existing test suite must stay green
- Do not change unrelated behaviour
- Default max_body_size=None must preserve current unlimited behaviour

## Repository facts

- Test files are those matching: tests/**/*.py
- Tests are run by the harness, not by the implementer.


## What the plan must contain

Return JSON matching the schema you were given.

- `intents`: every intent id above, exactly once, with a `resolution`:
  - `change`: something must change. `evidence` is optional but welcome.
  - `already_satisfied`: the repository already does it. Requires `evidence` that proves it.
  - `out_of_scope`: you propose not doing it. Requires a `rationale`; a human will decide.
- `evidence`: `path` relative to the repository root, `start`/`end` line numbers (1-based,
  inclusive) and a short `quote` that appears literally within those lines. The harness checks
  the file, the range and the quote; a wrong quote fails the plan.
- `criteria`: acceptance criteria, each tied to an `intent`. `kind` is `behavior` when a test
  can prove it, `other` otherwise (documentation, naming). Write them so that a test can be
  named for each `behavior` criterion.
- `tasks`: the units of work. Each task must fit a single short session with a fresh context:
  - `files`: every file the task may touch, with `role` (`test` if it matches the test globs,
    `production` otherwise) and `action`. The implementer is blocked from touching anything
    else. Declaring a file you will not need is cheap; forgetting one costs a replan.
  - `tests`: the test files (existing or to be created) that prove this task's criteria. Every
    `behavior` criterion must be covered by at least one task with at least one test. **Every
    path in `tests` must also appear in that task's `files`** (with `role: test`); a test path
    missing from `files` fails the plan.
  - `criteria`: the criteria keys this task satisfies.
  - `depends_on`: keys of tasks that must be committed first. No cycles.
  - Split the work into tasks along real dependencies: a change that several other changes build
    on (a signature, a new method, a data structure) goes first as its own task, and the tasks that
    rely on it declare it in `depends_on`. Two tasks may touch the same production file only when
    one depends on the other; independent tasks must not share a production file. A small change
    that fits one session stays one task; do not split for the sake of splitting.
  - Order by dependency, not by topic: a deprecation, removal or behaviour change of something
    that other code still calls must come **after** the tasks that migrate those callers (and
    their tests), or the suite goes red (many suites turn warnings into errors). Add the new
    thing first, migrate its users next, deprecate the old thing last.
  - Existing tests that assert the old behaviour must be listed in the `files` (role `test`) and
    `tests` of the task that changes that behaviour; the implementer is blocked from touching any
    test file the task does not declare.
- `summary` on the plan and on each task: one or two sentences for a human reviewer.

Do not include anything the user did not ask for (docs, refactors, extra features) unless an
intent requires it; if you think it is needed, say so in the plan summary instead.
