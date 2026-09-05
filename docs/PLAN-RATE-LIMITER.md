# Implementation plan: shared adaptive rate limiter

**Status: implemented.** Code, tests and documentation have landed; the one
outstanding item is the measured run against a real mailbox described under
[Verification](#verification), which needs a live inbox and real quota. The
post-limiter row in `DESIGN-UI.md` stays empty until then.

This document is kept as written, in the present tense of a proposal, because
it records *why* each choice was made. Where it says "do not revisit", that
still holds. This is Phase 1 of
[`DESIGN-UI.md`](DESIGN-UI.md). That document states *what* and *why*; this one
states *how*. It is contributor-facing — read `CLAUDE.md` first for the
invariants, the offline test loop and the platform gotchas.

---

## Context

The browser UI in `DESIGN-UI.md` deliberately phases the rate limiter first,
before any server code. The reason is in the measurements that document
records: a short burst reaches 35.7 msg/s at concurrency 16, but the same work
sustained over 53 minutes collapses to **5.1 msg/s**.

The cause is neither the Gmail quota itself nor subprocess overhead — the burst
figure was achieved *through* the same subprocess path. It is that `gws()`
(`gmail_audit.py:70-96`) gives every worker its own `delay` local. Twelve
workers each discover the per-minute ceiling independently and each sleep up to
60 seconds alone, none of them telling the others. The fleet spends most of its
wall clock asleep rather than near the ceiling.

Fixing this first makes the rest of the UI work tractable: every later phase is
easier to build and test when a full scan takes minutes instead of an hour. It
also carries no UI risk — no server, no browser, no new attack surface.

**Outcome:** a full-inbox fetch sustains >20 msg/s end-to-end with drops
counted rather than silent, and its progress is legible while it runs.

### Decisions taken — do not revisit during implementation

1. **Token bucket over the existing `gws` subprocess path.** No direct HTTPS,
   no `urllib`, no reading OAuth credentials. Two reasons beyond diff size:
   `gws auth export` would put a refresh token in this tool's memory, where no
   credential lives today; and routing around `gws` **bypasses `--sanitize`
   entirely**, since Model Armor is a `gws` service. That is a safety
   regression `DESIGN-UI.md` does not mention. This resolves that document's
   own open question ("Handling the refresh token"), which asks to be decided
   explicitly rather than drifted into.
2. **Verification by fake clock and fake transport** — offline, deterministic.
3. **Dropped messages: counted, summarised, and written to a retry file.**
   Exit code stays 0.

---

## Approach

### The limiter: a deadline scheduler, not a counter

Implement the token bucket in **GCRA form** — one scalar "theoretical arrival
time" plus a burst tolerance — guarded by a plain `threading.Lock`.

This choice *is* the design, because it prevents a thundering herd rather than
dispersing one. With a counter and a `threading.Condition`, a waiter cannot
compute its own wake time — whether a token is free depends on which other
waiters take one first — so it must loop wake, recheck, re-wait, and
`notify_all` wakes all twelve workers for a single token. With deadlines, token
accrual is a pure function of wall time, so a caller can atomically **claim**
the next departure slot under the lock and then sleep alone until its own
private instant. No waiter ever wakes to find its slot taken. The herd cannot
form.

Split acquisition in two. This is the seam that makes everything else testable:

```python
def reserve(self):   # pure state transition over an injected clock; never sleeps
def acquire(self):   # reserve(), then sleep until the deadline in <=1s slices
```

Because `reserve()` never sleeps, fleet behaviour can be tested with no threads
and no fake sleeper — a single-threaded discrete-event loop that advances
virtual time itself.

### Adaptive control: AIMD with three guards

Split the conflated `RETRYABLE` regex (`gmail_audit.py:63-67`) in two, because
the two failure modes call for opposite responses:

- `THROTTLE` (429, quota) — **the fleet is too fast.** Shrink the shared rate,
  re-queue, and take **no local sleep**. This is the behavioural core of the
  phase.
- `TRANSIENT` (5xx, backend error) — **this request failed.** Keep the existing
  per-request exponential backoff; the rate is not implicated and must not move.

Add word boundaries while splitting. Measured against the current pattern:

```
'id 18f4429ab0c not found'     old: retryable=True   new: throttle=False
'message 500abc123 malformed'  old: retryable=True   new: throttle=False
'HTTP 429 Too Many Requests'   old: retryable=True   new: throttle=True
```

Gmail message IDs are lowercase hex, so the bare `429`, `500` and `503`
alternatives can match an ID inside an error string and treat a hard failure as
retryable, burning six retries and up to two minutes. Whether `gws` echoes the
ID into stderr in practice is unverified; the boundaries are cheap hardening
either way.

Three guards that textbook AIMD lacks:

1. **Only raise the rate when the limiter is actually binding.** If no worker
   waited, the constraint is `--concurrency` or Gmail latency, not the limiter.
   Raising anyway builds unearned credit, spent later as an overshoot burst the
   moment latency improves.
2. **Hold ~15s after any decrease**, bounding the sawtooth period and giving
   the API's own averaging window time to drain.
3. **Coalesce throttles inside a ~2s window into one decrease.** When the rate
   is over the ceiling, the API throttles most of the ~12 in-flight workers
   within a few hundred milliseconds. A naive per-event decrease gives
   `0.7^12 = 0.014` — 35 req/s down to 0.5 req/s from a single overshoot. This
   is the direct answer to "one worker's 429 must not tank the fleet". Extra
   throttles are still *counted* for reporting; they simply do not compound.

Constants: start 8.0 req/s, min 1.0, max 40.0, burst 4, +1.0 req/s every 3s,
x0.7 on throttle, 1.0s fleet-wide pause on throttle, 15s ramp hold. The maximum
sits between the last clean measurement (35.7 msg/s) and the first that dropped
messages (44.6 msg/s). Predicted steady state around a true ceiling C is
~0.80*C — about 28 msg/s at C=35, clearing the >20 msg/s bar with margin.

The sawtooth is not a defect, it is the mechanism: the ceiling cannot be
queried, so it must be repeatedly re-found. What matters is that the guards
bound it.

### Placement: a module-global singleton

`LIMITER` as a module global set in `main()`, with a `limiter=` override
parameter on `gws()` for tests. Not threaded through call signatures, for a
semantic reason rather than convenience: **the quota is per-process-per-user,
so two limiters in one process is a bug, not a configuration.** `GWS`
(`gmail_audit.py:51`) is already this kind of resolved-once module handle; the
limiter is its sibling.

It is also the only option that survives `cmd_fetch` recreating its
`ThreadPoolExecutor` per 1000-message batch (`gmail_audit.py:219-221`) without
touching `_safe`'s signature. Per-pool state would reset the learned rate and
re-ramp from 8 req/s roughly 35 times over a full inbox.

Nothing else needs wiring: `_safe` -> `get_headers` -> `gws` already funnels
every worker through one function, so `cmd_engaged`, `cmd_trash` and
`cmd_untrash` get the limiter for free.

---

## Implementation

All in `gmail_audit.py` unless noted. Standard library only, **Python 3.8
compatible** — a newer interpreter may be installed locally, so do not let
3.9+ syntax slip in.

### 1. `RateLimiter` — new class, near `gws()`

Constructor `(rate, burst, min_rate, max_rate, adaptive, clock=time.monotonic,
sleeper=time.sleep)`. Methods: `reserve()`, `acquire()`, `shutdown()`,
`on_success()`, `on_throttle()`, `on_server_error()`, `on_error()`, a `rate`
property and `stats()`.

`reserve()` under the lock: `T = 1/rate`, `tau = (burst-1)/rate`, advance the
stored arrival time, return the deadline, and set a `_waited_since_probe` flag
when the wait was non-zero — that flag drives guard 1. Note the tolerance is
`(B-1)/R`, not `B/R`; an easy off-by-one, pinned by a test.

`on_throttle()` applies the fleet-wide pause as `_tat = max(_tat, now + PAUSE)`,
which delays only *future* acquirers. Workers already holding a deadline
proceed, so overshoot is bounded by `--concurrency` requests. Revoking issued
deadlines would need a `Condition` and buy nothing.

### 2. Split the retry regex — replaces `RETRYABLE` at `:63-67`

`THROTTLE` and `TRANSIENT` as above, plus a `_retryable(stderr)` helper.

### 3. Extract `_run()` and `_sleep` seams, rewrite `gws()` at `:70-96`

`_run(cmd)` becomes the single place this module spawns a subprocess, keeping
the UTF-8 forcing and its comment — the Windows cp1252 default raises
`UnicodeDecodeError` mid-fetch. `_sleep = time.sleep` as a module attribute.
Both exist so tests can replace them by assignment, with no mocking library.

The rewritten `gws()` keeps **separate retry budgets**: `throttle_retries=12`
with *no* local sleep, because `limiter.acquire()` is now the backoff; and the
existing `retries=6` with exponential backoff for transient errors only. With
the local sleep gone, reusing a budget of 6 for throttles would burn in about
six seconds and drop the message.

### 4. `FetchProgress` and a reporter thread

`FetchProgress` holds `total`, `done`, `dropped`, a start time and a deque
window for instantaneous rate; `snapshot()` returns a plain dict. Stored as a
module global *and* on `a.progress`, so a future `cmd_ui()` can serve
`/api/progress` in-process without a file.

A daemon thread ticking every 2s, started before the batch loop at `:219` and
stopped via a `threading.Event` in a `finally`. It must be a thread, not a
print inside the `ex.map` consumer: printing from the consumer freezes during a
global pause, which is the exact pathology being fixed. `DESIGN-UI.md` asks
that a stall read as "waiting on quota, resuming in 12s", not as a frozen
counter.

Line format — stderr only, carriage-return rewritten when `isatty()`, otherwise
plain newlines every 10s so `run.log` stays greppable:

```
  4,213/35,102  12.0%   28.4 msg/s (avg 24.1)  limit 31.0/s ramping     eta 18m12s  drops 0
  4,401/35,102  12.5%    0.0 msg/s (avg 23.8)  limit 21.7/s backoff 4s  eta 21m03s  drops 0  thr 3
```

State is one of `ramping | holding | backoff Ns | pinned | at-max | FLOOR`.
`FLOOR` is capitalised because a limiter pinned at the minimum means something
is badly wrong and should not look routine. ETA derives from observed
throughput, not a benchmark. Keep the existing per-batch line (`:226-231`) as a
coarse checkpoint that survives the carriage-return overwriting.

Keep `ex.map`. Switching to `as_completed` would reorder writes into
`headers.jsonl` for no benefit — nothing depends on that order, but it is a
gratuitous observable change.

### 5. Drop tracking — `_safe()` at `:194-199`

Add an optional `on_drop=None` callback rather than another global. `_safe` has
exactly two call sites (`:222`, `:245`) and both already build a lambda.
Behaviour when it is `None` stays exactly as today.

`cmd_fetch` passes `progress.record_drop`, which increments under the lock,
appends a line, and prints at most the first 20 individual failure lines
followed by a suppression note, so a systemic failure does not flood stderr
with 30,000 lines.

`cmd_engaged` should pass it too. A drop there silently weakens the replied-to
safeguard, and the existing empty-list guard (`:257-263`) catches only total
failure, not partial.

File: `fetch-dropped.jsonl` (`--dropped`, empty string disables), and
`engaged-dropped.jsonl` for the engaged scan. One object per line —
`{"id", "ts", "error"}` with `error` truncated to 200 characters. No headers,
no subject: the file names what to retry, it does not archive content. Written
incrementally and flushed per line, since an interrupted run is exactly when it
matters. Created lazily on the first drop, so a clean run leaves no file at all.

**The `.jsonl` extension is a safety requirement, not a style choice.**
Verified:

```
$ git check-ignore -v fetch-dropped.jsonl
.gitignore:8:*.jsonl    fetch-dropped.jsonl
$ git check-ignore -v dropped-ids.txt
(not ignored)
```

The file contains real message IDs — the same class of data as `headers.jsonl`.
No `.gitignore` change is needed, but a rename to `.txt` would silently make it
committable, which is why a test pins the extension.

End-of-fetch summary, exit code 0:

```
  35,102 requested, 35,075 cached, 27 not fetched

WARNING: 27 of 35,102 messages could not be fetched after retries.
  Sender counts from this run are INCOMPLETE - the ranking will undercount.
  IDs written to: fetch-dropped.jsonl
  Re-run the same fetch command to retry them; the cache resumes by diffing
  IDs, so nothing already fetched is re-requested.
```

Print the `requested / cached / not fetched` reconciliation **even on a clean
run** (`len(load_cache(a.cache))` against `len(ids)`). It is three lines, and it
is the only check that catches a *silent* undercount as well as an
error-counted one. See Risks.

### 6. Consecutive-failure circuit breaker

Abort the fetch loudly after ~25 consecutive non-retryable failures, pointing at
`gws auth login`. `SETUP.md` documents that External/Testing refresh tokens
expire after seven days, so a long run can lose auth mid-flight. An auth error
matches neither regex, so today the loop churns through every remaining ID at
full speed — and after this change would also write a 30,000-line drop file.
This converts the worst realistic failure into one clear line.

### 7. New flags on `fetch`, `engaged`, `trash`, `untrash` (`:582-620`)

| Flag | Default | Meaning |
|---|---|---|
| `--rate` | `0` (adaptive) | Pin a fixed rate. Throttles still pause but never shrink |
| `--max-rate` | `40.0` | Ceiling on the adaptive search — the knob for "quota ceilings vary" |
| `--start-rate` | `8.0` | Advanced; tests and repeat runs |
| `--no-rate-limit` | off | Escape hatch restoring pre-Phase-1 behaviour, useful for a bisect |

**Raise the `--concurrency` default from 8 to 12** (`:586`). Concurrency now has
one job, covering latency (`workers ~= rate * 0.35s`), so 8 workers cap
throughput at about 23 req/s regardless of what the limiter permits. Keep 16 as
the documented maximum: the "API drops messages above ~16" pathology is a
*concurrency* effect that the limiter does not repeal. Print the implied ceiling
at fetch start so the coupling is visible.

---

## Tests — `tests/test_audit.py`

Match the existing style exactly: plain asserts, `test_` prefix, no pytest
fixtures, no mocking library, picked up by the hand-rolled runner at `:167-178`
which collects every global starting with `test_` — so helpers must not.

**Seams**, all assigned directly with `try/finally`: `g._run`, `g._sleep`,
`g.LIMITER`, and the `clock=` / `sleeper=` constructor parameters. Because
`cmd_fetch` reaches the network only via `list_ids` -> `gws` -> `_run` and
`_safe` -> `get_headers` -> `gws` -> `_run`, **replacing `g._run` alone gives
full offline control of an entire `cmd_*` call.** That is the main payoff of
extracting it.

Helpers: a `_Proc` stand-in for `CompletedProcess`; a `_FakeTransport` that
inspects argv, so one instance serves `messages list` and `messages get` within
a single call; a one-field `_Clock`; and `_simulate()`, a heap-based
discrete-event fleet loop against a virtual API that returns 429 when arrivals
over the trailing second exceed a ceiling. Runs in single-digit milliseconds.

| Group | Tests |
|---|---|
| Mechanics | paces at the configured rate; burst admits exactly B; **12 concurrent reservations yield 12 strictly increasing, distinct deadlines** (the herd property, as an assertion); a rate change takes effect on the next `reserve()`; the rate never reaches zero |
| Adaptive | 12 simultaneous throttles decrease the rate **once**, not by `0.7^12`; 50 server errors leave the rate unchanged but one throttle shrinks it; no increase when the limiter is not binding; a pinned rate ignores adaptation |
| Regex | throttle and transient are distinguished, and `THROTTLE` does not match an error string containing a hex message ID |
| `gws()` | a 429 retries with `_sleep` never called; a 503 does sleep and leaves the rate unchanged |
| Sharing | limiter state survives executor recreation — deadlines in a second `ThreadPoolExecutor` block continue from the first rather than restarting, and a reduced rate persists across the boundary (the exact failure a per-pool limiter would show at `:221`); `gws` uses the module limiter by default |
| Drops | `cmd_fetch` records dropped IDs and continues — 4 of 5 cached, one valid JSONL line, exit 0; the default drop filename ends in `.jsonl` and `.gitignore` carries a bare `*.jsonl` rule |
| Headline | **the fleet settles near a simulated ceiling** — sustained throughput over the last 60 virtual seconds within `[0.6, 1.05] * ceiling`; repeated at `ceiling=35` asserting >20 msg/s, which makes `DESIGN-UI.md`'s own done-when criterion an offline assertion |

Test count goes from 11 to roughly 28. All 11 existing tests must still pass
unchanged — in particular `test_no_permanent_delete_code_path`, which greps the
source.

**Honest limitation:** these prove the *schedule* is herd-free by construction.
They do not prove the absence of lock convoy under real OS threads. Test the
claim you can make, and do not claim more than that in the commit message.

---

## Documentation to update

- **`DESIGN-UI.md`** — label the measurement table as pre-Phase-1 and mark the
  5.1 msg/s row **historical**, which its own "Keeping this document honest"
  section explicitly asks for. Leave the post-limiter row empty until a real
  run fills it; do not write in a projected number. Mark Phase 1 shipped in the
  phasing table, rewrite the Phase 1 implementation note from proposal to
  description, and **record the no-direct-HTTPS decision** with its rationale.
- **`CLAUDE.md`** — rewrite the "Gmail quota is per MINUTE" note into past
  tense for the backoff pathology; add `RateLimiter` / `LIMITER`, `THROTTLE` /
  `TRANSIENT` and `FetchProgress` to the symbol table; update the test count.
- **`SETUP.md`** — in the rate-limits section, keep the concurrency table as a
  labelled historical measurement and replace "Stay at 12-16" with the new
  model: the limiter governs rate, concurrency only covers latency, and never
  above 16. Document the new flags, the progress-line states and
  `fetch-dropped.jsonl`. Update the "Quota exceeded" troubleshooting entry —
  transient `backoff` is now normal; a persistent `FLOOR` means the project
  quota is genuinely lower, so pin `--rate`.
- **`README.md`** — the rate-limits section, dropping "dropping concurrency is
  the real fix", which is no longer true; move the global rate limiter from
  planned to shipped in the roadmap.

---

## Verification

Both suites run offline against fixtures, with no Gmail access and no quota
cost. This is the development loop.

```bash
python tests/test_audit.py
```

```bash
python tests/check_diagrams.py
```

Then exercise the ranking path against the synthetic fixture:

```bash
python gmail_audit.py rank --cache tests/fixtures/headers.jsonl
```

Finally, **one measured run against a real mailbox** to fill in the
post-limiter row `DESIGN-UI.md` asks for. Use a bounded slice, not the whole
inbox:

```bash
python gmail_audit.py fetch --limit 5000 --concurrency 12
```

Pass criteria, from `DESIGN-UI.md`: sustains **>20 msg/s end-to-end** and
reports **zero dropped messages**. A run that is fast but drops messages is a
failure, not a partial success. Record the observed number in `DESIGN-UI.md`.

Per the privacy rules in `CLAUDE.md`: do not paste output containing real
sender addresses or message IDs anywhere, and check `git status` before
committing. `headers.jsonl` and `fetch-dropped.jsonl` are gitignored, but the
habit is the safeguard.

---

## Risks and deferred items

**`list_ids` is invisible to the limiter.** It uses `--page-all --page-limit
200`, so one subprocess performs roughly 70 API calls (~35s) and is granted a
single token. The fetch therefore begins immediately after a burst of unpaced
traffic that has already drained the quota bucket, making the first seconds the
likeliest to throttle. A start rate of 8.0 partly covers this. A fuller fix —
charging N tokens, or paging explicitly — is out of scope here.

**"Zero dropped messages" may not be checkable the way `DESIGN-UI.md` implies.**
`CLAUDE.md` says concurrency above ~16 makes the API "drop messages outright".
If those surface as errors, the drop tracking counts them; if the API returns
success with missing data, nothing in Phase 1 sees them. The end-of-fetch
reconciliation catches both, which is why it should print unconditionally. This
gap is in the design, not only in the code.

**Interaction with a moving-average quota window** is the likeliest reason this
under-delivers. If Gmail enforces an average over ~60s rather than an
instantaneous rate, a ~47s AIMD sawtooth can sit under the instantaneous limit
while exceeding the windowed average, producing throttles that look
inexplicable. The chosen constants put the 60s average at about 0.80*C, and the
15s ramp hold is deliberately a meaningful fraction of a minute. The proper fix
is a two-level limiter — a fast bucket at the current rate plus a slow bucket
over a 60s window. **Do not build it in Phase 1;** name it as the next step if
measured throughput lands well below the predicted 0.80*C.

**Quota cost is not uniform.** `messages.get`, `list` and `trash` cost 5 units;
`getProfile` costs 1. Pacing requests is a fine proxy within `fetch`, which is
homogeneous, and approximate when the same singleton also governs `cmd_trash`.
An `acquire(cost=)` parameter is deferred.

**Two processes, one quota.** Nothing stops a user running `fetch` and
`engaged` simultaneously, giving two independent limiters searching one
ceiling. AIMD is designed for exactly this and converges to a roughly fair
split, so it degrades gracefully — but it will look like the ceiling halved,
with no hint why. Worth one sentence in `SETUP.md`.

**Ctrl-C latency.** `ThreadPoolExecutor.__exit__` joins running futures, and
`cancel_futures` needs Python 3.9 while this targets 3.8. A worker can also be
sleeping in `acquire()` for up to `concurrency / RATE_MIN` — about 12s at the
floor. A minimum rate of 1.0 bounds it, and sleeping in short slices makes
`shutdown()` observable; wire it into a `KeyboardInterrupt` handler in
`cmd_fetch`. A scan you cannot interrupt is its own kind of invisible.

**A maximum of 40 req/s is calibrated on one project.** `--max-rate` is the
answer, and because 40 is only a cap on the search rather than a target, a
lower real ceiling is discovered automatically. The failure mode runs only in
the other direction: a project with a higher ceiling is left at 40.

---

## Not in this phase

Phases 2-5 of `DESIGN-UI.md` — the `ThreadingHTTPServer`, preflight, the review
table, the trash and undo endpoints, and incremental History API scans. No HTTP
surface, no per-launch token and no HTML is written here.

The one forward commitment is that `FetchProgress.snapshot()` and
`RateLimiter.stats()` return plain dicts, so a later `/api/progress` can serve
them in-process without a file.

---

## What shipped, and where it differs from this plan

The implementation follows the plan. Three things are worth recording:

**Test count landed at 33, not the estimated 28.** All 11 original tests pass
unchanged, including `test_no_permanent_delete_code_path`.

**The herd-property test uses `burst=1`.** With the burst tolerance in place
the first B reservations all clamp to "now" and are therefore equal, so the
"12 strictly increasing, distinct deadlines" assertion only means anything
outside the burst allowance. Burst behaviour is pinned separately by
`test_limiter_burst_admits_exactly_b`.

**A virtual clock needs a sleeper that advances it.** `acquire()` sleeps in
slices and re-reads the clock, so a frozen clock plus a no-op sleeper spins
forever. `_Clock.sleeper` in the test file exists for that; any test that
exercises `acquire()` rather than `reserve()` must pass it.

The fleet simulation settles at 0.75-0.99x the simulated ceiling, bracketing
the predicted ~0.80*C. At ceiling 35 it is concurrency-bound rather than
rate-bound (12 workers / 0.35s = 34 req/s), which is the expected coupling and
is why `fetch` prints the implied ceiling at startup.

A separate threaded smoke run against a fake transport confirmed the coalescing
guard under real OS threads: seven simultaneous throttles produced a single
decrease, 8.0 -> 5.6 req/s, not `8 * 0.7^7`. That is stronger evidence than the
deterministic tests can give, but it is still not proof of the absence of lock
convoy under load - as the Tests section says, do not claim more than that.
