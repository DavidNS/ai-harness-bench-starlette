An independent reviewer, who has not seen your reasoning, blocked task T1 (fix-up 1). Fix the findings below ON TOP of your current change: do not start over, do not revert what works, and touch only the task's files. Where a finding says a case is untested, add the test; where it says the code is wrong, fix the code and test it.

Findings:
- the reviewer requests 2 change(s): `content_length.isdigit()` is true for non-ASCII digits such as latin-1 b'\xb2' ('²'), and then `int()` raises ValueError. A malformed Content-Length crashes the request instead of being ignored. Use `isascii() and isdigit()` or a try/except.
- [pass 1] starlette/middleware/body_limit.py:24: `content_length.isdigit()` is true for non-ASCII digits such as latin-1 b'\xb2' ('²'), and then `int()` raises ValueError. A malformed Content-Length crashes the request instead of being ignored. Use `isascii() and isdigit()` or a try/except.
- [pass 2] tests/middleware/test_body_limit.py:0: No test covers the `send()` path where a declared over-limit Content-Length meets an app that responds without ever calling receive(). The 413 must replace the app's response there. The 'declared too large' tests all use apps that read the body.

When done, finish again with the same JSON answer (summary, criteria_notes, replan_request). The harness will run the task's tests and review the change again.