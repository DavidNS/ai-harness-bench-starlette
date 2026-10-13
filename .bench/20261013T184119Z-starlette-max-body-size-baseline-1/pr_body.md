Bench arm **baseline** on case `starlette-max-body-size` (intent v2), run `20261013T184119Z-starlette-max-body-size-baseline-1`.

`claude -p` with the case intent, edits auto-accepted, shell limited to python/pytest, model `claude-sonnet-5-5`. No harness, no gates, no review.

| | |
|---|---|
| Judge | FAIL (FAIL_TO_PASS failed, PASS_TO_PASS ok) |
| Hidden tests | 4 failed, 32 passed in 0.36s | 104 passed in 1.56s |
| Suite | 14 failed, 903 passed, 2 skipped, 2 xfailed in 7.33s |
| Cost | 0.53 USD |
| Wall clock | 190 s |
| Output tokens | 21760 |
| Fixture base | `c41236c03868fb3779a64101f4ea88cd47877e23` |

Artifacts: `.bench/20261013T184119Z-starlette-max-body-size-baseline-1/` (CLI transcript JSON, diff, this row).

🤖 Generated with [Claude Code](https://claude.com/claude-code)