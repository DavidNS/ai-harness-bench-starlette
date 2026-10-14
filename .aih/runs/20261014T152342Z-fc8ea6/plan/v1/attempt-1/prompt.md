You are planning a change in this repository. You can read files but not edit them or run
commands. Another session will implement your plan, one task at a time, each in a fresh context, and a
harness will check the result against it mechanically. Explore the repository as you see fit;
only your final JSON answer is used.

## Request

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

## Intents (use these ids exactly)

- `INTENT1` (body-limit-middleware): Add RequestBodyLimitMiddleware in starlette/middleware/body_limit.py, usable with any ASGI app, that counts the body bytes actually received from the server and defaults to no limit.
- `INTENT2` (limit-enforcement-413): When received bytes exceed the limit, respond 413 with plain-text body `Content Too Large`; if the response has already started, send no second response and stop the body stream.
- `INTENT3` (content-length-fail-fast): Reject a declared Content-Length over the limit with 413 before reading the body, while an absent or understated Content-Length still gets enforced on the bytes actually received.
- `INTENT4` (max-body-size-option): Add a `max_body_size` option (default None, meaning unlimited as today) to Starlette, Router, Mount and Route.
- `INTENT5` (limit-scoping-override): The application-level limit applies before routing, and a limit on an inner router, mount or route overrides the outer one for that part of the tree, whether it raises or lowers the outer limit.
- `INTENT6` (multipart-limit-and-cleanup): Multipart bodies count toward the limit (file data and encoding overhead included), and multipart parsing closes its temporary files whenever a parse is interrupted, including when the body limit is crossed.
- `INTENT7` (document): Document the `max_body_size` option and RequestBodyLimitMiddleware in the docs.
- `INTENT8` (tests): Add tests for the limit on the app, router, mount and route (including inner limits raising and lowering the outer one), the standalone middleware, the Content-Length fail-fast, the multipart case and the temp-file cleanup.

## Constraints

- Default max_body_size=None must keep today's unlimited behaviour.
- The existing test suite must stay green.
- Do not change unrelated behaviour.

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
- `tasks`: the units of work. **A task is an increment that could be released on its own: the
  change that would ship as one pull request.** A request that ships as one PR is ONE task, however
  many files it touches; splitting it into steps costs a fresh context per step, loses the design
  decisions between them and produces nothing releasable in between. Use several tasks only when
  the request contains pieces that could be merged and released independently of each other
  (then `depends_on` says which must land first). Never split by layer, by file or by phase.
  - `files`: every file the task may touch, with `role` (`test` if it matches the test globs,
    `production` otherwise) and `action`. The implementer is blocked from touching anything
    else. Declaring a file you will not need is cheap; forgetting one costs a replan.
  - `tests`: the test files (existing or to be created) that prove this task's criteria. Every
    `behavior` criterion must be covered by at least one task with at least one test. **Every
    path in `tests` must also appear in that task's `files`** (with `role: test`); a test path
    missing from `files` fails the plan.
  - `criteria`: the criteria keys this task satisfies.
  - `depends_on`: keys of tasks that must be committed first. No cycles. Independent tasks must
    not share a production file.
  - Existing tests that assert the old behaviour must be listed in the `files` (role `test`) and
    `tests` of the task that changes that behaviour; the implementer is blocked from touching any
    test file the task does not declare.
  - In the task `summary`, state the order of work the implementer should follow when it matters
    (add the new thing, migrate its callers and their tests, deprecate or remove the old thing
    last: many suites turn warnings into errors).
- `summary` on the plan and on each task: one or two sentences for a human reviewer.

Do not include anything the user did not ask for (docs, refactors, extra features) unless an
intent requires it; if you think it is needed, say so in the plan summary instead.
