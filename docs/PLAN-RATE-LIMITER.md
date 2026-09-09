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

---

## Measured against a real mailbox, 2026-09-08: the adaptive controller fails

Everything above this section was validated in simulation. The first run
against a real mailbox contradicts it. **The adaptive limiter is strictly
worse than a fixed rate on that mailbox, and it collapses to `RATE_MIN` every
time.**

Environment: ~35,000-message inbox, 4,764 sent messages, Windows, `gws` over
Node, a personal Google Cloud project in Testing mode. Every run below scanned
the same 4,764 sent messages through `cmd_engaged`.

| Run | Throughput | Throttles/s | Throttled | Limiter rate |
|---|---|---|---|---|
| adaptive, concurrency 12 | 3.66 msg/s | 0.75 → 0.81 | 21% of attempts | 3.63 → **1.00 `FLOOR`** |
| adaptive, concurrency 4 | 3.91 msg/s | 0.48 | 11% of attempts | 8.0 → 2.58, still falling |
| **pinned `--rate 8`, concurrency 4** | **5.17 msg/s** | **1.31** | 20% of attempts | **8.00, stable** |

Phase 1's done-when required **>20 msg/s sustained with zero dropped
messages**. Throughput is missed by 4-5x, and the best result came from turning
the adaptive controller *off*.

**Drops were not zero either**, though they are rare: 6 messages across roughly
14,000 fetch attempts in four runs, 1-2 per run. An earlier draft of this
section claimed zero, from mid-run status snapshots that had not yet
accumulated any. The drop file is the authority, not a snapshot.

### Why it collapses, exactly

```
decrease gate (THROTTLE_COALESCE) : 2s   -> a decrease may fire every 2s
increase gate (THROTTLE_HOLD)     : 15s  -> blocked 15s after ANY decrease
observed throttle spacing         : 1.2s (c12) to 2.1s (c4)
```

Throttles arrive faster than the hold expires, so `_maybe_increase()` is never
reached. Only decreases fire, and the rate falls geometrically to `RATE_MIN`.

This is not a tuning error, it is a structural one: **the guard that bounds the
sawtooth is slower than the guard that permits the cut.** Any workload whose
throttles arrive more often than once per `THROTTLE_HOLD` collapses, at any
rate, on any mailbox. The three guards were each reasonable alone; guard 2
(hold after decrease) and guard 3 (coalesce decreases) were never checked
against each other.

### The deeper error: throttles here are a cost, not a signal

AIMD assumes a 429 means *the fleet is too fast*, so cutting the rate should
reduce throttling. Three measurements say otherwise:

1. **A 72% rate cut (3.63 → 1.00 req/s) raised throttle frequency by 7%.**
   The rate lever is not connected to the throttle source.
2. **Cutting concurrency 3x halved the throttled *fraction* (21% → 11%) but
   did not stop the collapse.** Parallelism contributes; it is not the trigger.
3. **The pinned run absorbed 2.7x more throttling than the adaptive one and
   delivered 32% more throughput, flat over eight minutes.** A throttled
   request retries and succeeds; the work still gets through.

So on this mailbox a 429 is a *cost to absorb*, not a signal to retreat. The
adaptive limiter has been paying the cost of throttling **and** the cost of
retreating from it, and the retreat buys nothing.

### Why the simulation could not have caught this

`_simulate()` in `tests/test_audit.py` models the quota as a **trailing
one-second window**: it throttles when arrivals in the last 1.0s reach the
ceiling. Under that model a rate cut immediately and proportionally reduces
throttling, so AIMD converges by construction. The test asserting >20 msg/s
was therefore asserting a property of the simulation, not of the limiter.

`CLAUDE.md` has said the whole time, in its own words, that **"Gmail quota is
per MINUTE, not per second."** The simulation encodes the shape the
documentation explicitly says is wrong. Whatever the real constraint is, it has
a component that request rate does not move.

### What to change

Items 3 and 4 are **done**, and item 3 is what produced the diagnosis in
"the throttle, diagnosed" below - read that before acting on 1 or 2, because
it changes what a correct controller would even be. The control law that
replaces AIMD-on-rate deserves designing rather than guessing: tuning
constants against a half-understood mechanism is how the current constants
were arrived at.

In rough order of confidence:

1. **Make the decrease gate no shorter than the increase gate.** If
   `THROTTLE_COALESCE >= THROTTLE_HOLD`, decreases cannot outpace increases and
   the collapse is structurally impossible. This is the minimal fix and it
   restores the equilibrium AIMD is supposed to find.
2. **Close the loop.** Only keep decreasing while the *throttle rate* is
   actually falling. After N decreases with no measured improvement, stop
   cutting and say so loudly rather than descending in silence. `FLOOR` should
   be a reported failure, not a resting state.
3. ~~**Record the throttle text.**~~ **Done, and it ended the guessing in one
   run, exactly as predicted.** `_record_throttle()` samples the first five
   throttle stderrs per run into the limiter, the status file and the drop
   file, and counts throttles per API method so a listing throttle is
   distinguishable from a per-message one. The answer is
   `Units per minute per user`, the budget is spent by successful calls, and
   all four standing hypotheses about the cause turned out to be wrong. See
   the last section of this document.
4. ~~**Consider making `--rate` the default and adaptive the opt-in.**~~
   **Done.** On the only real evidence that exists, a fixed rate is faster,
   stable, and simpler. That is an uncomfortable conclusion for a document this
   long, and it is still the one the numbers support.

   A scan now pins `RATE_DEFAULT` (8.0 req/s); `--adaptive` opts back in and
   says in its own help that it is broken. `--rate 0` still means "adapt". The
   two are mutually exclusive, because silently letting one win is how a run
   ends up paced by something the operator did not choose.

   8.0 is not a tuned optimum. It is the one rate observed to be stable under
   load, and the ceiling is unmeasured: no run has been made at 12 or 16 to
   find where pinning starts drawing throttles faster than it clears requests.
   That measurement is worth more than any further work on the controller,
   because it bounds what a correct controller could even achieve. Until then a
   user with more quota raises it by hand with `--rate`.

### A second measurement: the mutation path is latency-bound, not quota-bound

A 1,108-message restore on 2026-09-09, pinned at the new default of 8 req/s
with concurrency 8. Each message costs two API calls, `messages.untrash` then
an add-only `messages.modify`:

| | |
|---|---|
| API calls | 2,216 |
| elapsed | 428.8 s |
| **calls/s** | **5.17** |
| concurrency | 8 |
| implied mean latency per call | **1.55 s** |
| **throttles** | **0** |
| limiter state | `pinned` at 8.0/s, never binding |

Two things fall out of that, and the second is more interesting than the first.

**The pin was never reached.** The limiter was set to 8 req/s and the fleet
managed 5.17. Nothing was waiting on the limiter, so the ceiling here is
`concurrency / latency`: eight workers each spending about 1.55 seconds per
call. That is subprocess cost, not quota. It is the strongest evidence yet for
the note under "Two things the drop file also exposed" - `gws` loads its
keyring on every invocation, and the tool spawns one process per message.

**Zero throttles, at a call rate that throttled the scan.** The pinned fetch
run reached 5.17 msg/s and was throttled 646 times, about 20% of requests. This
run made calls at the same rate and drew none at all. Both estimate to roughly
1,550 units per minute if every call costs 5, which is far under the documented
per-user ceiling either way.

So the fetch throttling is not explained by the steady-state call rate, and the
obvious suspects are the ones the fetch path has and this one does not:
`list_ids` pagination running alongside the fetch, and concurrency 12 rather
than 8. Neither is measured. What this run does establish is that a sustained
5.17 calls/s against a real mailbox is not, by itself, enough to draw a single
throttle - which means the earlier runs' throttling has a cause still not
identified, and the AIMD controller was reacting to something it never
diagnosed.

**Do not read this as "the limit is 5.17/s".** It is where *this* client tops
out at concurrency 8, entirely because of how it talks to the API.

### What survives

The parts of this design that are not the controller came through intact:

- **Drop accounting works, and is what finally identified the throttle.** Six
  drops across four runs, each with its ID, timestamp and error text on disk.
  The reconciliation and retry budgets behaved as designed.
- **The deadline scheduler.** No thundering herd, no lock convoy, no worker
  starvation was observed at any rate between 1.0 and 8.0 req/s.
- **The `THROTTLE` / `TRANSIENT` split.** Still correct, and now more clearly
  so: the two failures really are different, and the throttle branch is the one
  whose *response* is wrong, not its classification.
- **`--rate` as an escape hatch**, which is what produced the only usable run.
  `SETUP.md` already documented pinning as the response to `FLOOR`; that advice
  turns out to be the main path rather than a footnote.

### The throttle, finally read

Throttles are retried until they succeed, so their text never reached the drop
file and the counters were all we had. Two of the six drops exhausted all
twelve throttle retries, which put the message on disk for the first time:

```
error[api]: Quota exceeded for quota metric 'Total Query Cost'
            and limit 'Units per minute per user'
            of service 'gmail.googleapis.com'
```

**`Units per minute per user`.** `CLAUDE.md` had it right the whole time, and
`_simulate()` had it wrong: the constraint is a per-minute budget, not a
per-second rate.

That single word changes the shape of the problem. A per-minute bucket is
depleted by *cumulative units already spent in the current window*, not by the
instantaneous rate. Once the minute's budget is gone, every request fails until
the window rolls over — and **cutting the rate does not refund what was already
spent**. It only guarantees you are also slow when the fresh minute arrives.

That is precisely the measured behaviour: a 72% rate cut raised throttle
frequency by 7%. The limiter was reacting to a condition that only the clock
can clear. Worse, `THROTTLE_HOLD` is 15 seconds against a 60-second window, so
the limiter cuts up to four times inside a single window that no cut could have
rescued.

**AIMD on instantaneous rate is the wrong controller for this constraint.** A
controller that fits a per-minute budget would pace against the budget itself:
spend at most B units per window, track the window boundary, and treat a 429 as
"this window is spent, idle until it rolls" rather than "reduce the rate
forever." That is a different algorithm, not a retune, and it is why the fixes
listed above stop at preventing the collapse rather than claiming to solve it.

### Two things the drop file also exposed

**`gws` prints `Using keyring backend: keyring` to stderr on every single
invocation**, and it is prepended to every error the tool records. Four of the
six drops contain *nothing but* that line — a non-zero exit whose only stderr
was the startup chatter. Those match neither `THROTTLE` nor `TRANSIENT`, so
they were classified as hard failures, given zero retries, and counted toward
the circuit breaker. An unclassifiable failure carrying no API error at all is
more likely transient than permanent, and probably deserves the `TRANSIENT`
budget rather than immediate death.

**That line also means `gws` re-loads credentials from the OS keyring for every
message.** If a fresh process also refreshes or validates a token, the real
quota cost per message is higher than the documented 5 units for
`messages.get` — which would explain throttling at ~5 msg/s when the default
per-user budget of 15,000 units/minute should permit roughly ten times that.
This is a hypothesis, untested, and it bears directly on the "direct HTTPS
instead of subprocess-per-message — rejected" decision in `DESIGN-UI.md`. That
decision was taken on the grounds that subprocess overhead was latency the
limiter could absorb. If the overhead is *quota* rather than latency, the
premise was wrong and the decision deserves revisiting on its merits — with the
credential-handling and `--sanitize` objections still standing, unchanged.

> **Wrong, and settled the next day.** Both constants in that sentence were
> off: the budget is 6,000 units/minute per user, not 15,000, and
> `messages.get` costs 20 units, not 5. `6000 / 20 = 300` messages a minute
> is exactly the observed ceiling, so there is nothing left for a
> per-invocation keyring surcharge to explain. Subprocess cost is latency,
> as `DESIGN-UI.md` assumed, and that decision stands on its original
> grounds. See "the throttle, diagnosed" at the end of this document.

---

## Measured against a real mailbox, 2026-09-09: the throttle, diagnosed

Everything above this point about *why* the fleet gets throttled was inference
from counters. `gws()` held the API's own stderr at the moment it classified a
`THROTTLE` and threw it away, so the text quoted under "The throttle, finally
read" arrived only by accident: a message exhausted all twelve retries and
became a drop. With `_record_throttle()` keeping the first five texts per run
and counting them per API method, seven runs against the reference mailbox
settle the question.

**None of the four suspects is the cause.** The mechanism is a per-minute unit
budget spent by *successful* calls, and the puzzle that opened the previous
section - same client, same account, same call rate, opposite results -
dissolves once you notice that the two rows being compared are an *achieved*
rate and an *offered* rate, which are not the same measurement.

### How these runs were made

One process at a time, nothing else touching the account (checked against the
process list before and after), `--limit 2000` every run, over a throwaway
cache deleted before and after each run so every message really was fetched.
Every run therefore did identical work on the same 2,000 oldest inbox messages
and varied exactly one pacing parameter. Ninety seconds idle between runs,
because the quota is per MINUTE and one run's last minute of spending would
otherwise land inside the next run's first minute and be charged to it.

Each run also enumerated the whole inbox first - about 35,000 messages, 70
pages, roughly 35 seconds - because `cmd_fetch` lists before it fetches.

### The runs

| run | rate | conc | throughput | successes/min | attempts/min | throttles | lost | clean for |
|---|---|---|---|---|---|---|---|---|
| `r5c4`  | 5  | 4  | 5.00 msg/s | 300 | 300 | **3** | **0** | 144s |
| `r6c4`  | 6  | 4  | 5.24 msg/s | 314 | 354 | 251 | 1 | 92s |
| `r8c4`  | 8  | 4  | 5.16 msg/s | 310 | 385 | 488 | 2 | 40s |
| `r12c4` | 12 | 4  | **5.70 msg/s** | 342 | 428 | 504 | 2 | 22s |
| `r16c4` | 16 | 4  | 5.61 msg/s | 337 | 427 | 536 | 1 | 20s |
| `r8c8`  | 8  | 8  | 5.54 msg/s | 332 | 478 | 875 | 3 | 65s |
| `r8c12` | 8  | 12 | 5.57 msg/s | 334 | 479 | 868 | 3 | 63s |

"lost" is messages the run could not fetch after all twelve throttle retries,
read off the drop file. Every one failed for quota, not for anything about the
message. "clean for" is how long the run went before its first throttle.

Read the last four columns together, because that is where the mechanism is.
**Successes per minute barely move**: 300 to 342, a 14% spread across a 3.2x
range of pinned rate and a 3x range of concurrency. **Attempts per minute move
a lot**: 300 to 479. Turning either knob up buys rejected requests, not
messages.

### What the throttle actually says

Identical in all seven runs and in every one of the 33 sampled texts:

```
Using keyring backend: keyring
error[api]: Quota exceeded for quota metric 'Total Query Cost' and limit
            'Units per minute per user' of service 'gmail.googleapis.com'
            for consumer 'project_number:<id>'.
```

`<id>` is `redact()` doing its job. A project number is twelve digits and every
digit is also a hex digit, so the message-ID pattern claims it. That is the
right outcome for text meant to be pasted into this document, and it is why no
consumer number appears here.

### The four hypotheses

**H1 - the throttled runs shared per-user quota with a second process.
Disproved.** `r8c4` is the same configuration as the run that drew 646
throttles, run with nothing else touching the account: 5.16 msg/s against the
recorded 5.17, and 488 throttles against 646. The throttling reproduces solo.
Contention would of course make it worse, since the budget is per user, but it
is not needed to explain anything and the original run does not require a
second process.

**H2 - `messages.get` costs more units than `untrash`/`modify`. CONFIRMED, and
it is the whole answer.** Google's published table: `messages.get` costs **20
units**, `messages.untrash` and `messages.modify` cost **5** each. Equal call
rates are not equal quota rates, exactly as the hypothesis said.

This document originally argued the opposite here, and the argument was wrong,
so it is worth naming the error: it reasoned that if `get` were dearer, the
fetch's sustainable calls/min would have to be *lower* than the restore's, and
observed that both sat near 310/min. But the restore was never at its ceiling.
At 5 units a call its ceiling was 1,200 calls/min; it ran at 310 because it was
latency-bound, using **26% of the budget**. Two workloads landing at similar
calls/min tells you nothing about their unit cost when only one of them is
against the wall. Checking a hypothesis against a number that was free to be
anything is not a test.

**H3 - `list_ids` pagination bursting alongside the per-message fetch.
Disproved twice over.** The new per-method split counted **zero** `list`
throttles across seven runs; all 3,000-odd were `get`. And the burst cannot
overlap the fetch anyway: `cmd_fetch` runs `list_ids` to completion before the
first `messages.get`, and `list_ids` passes `--page-all`, so the whole 70-page
enumeration is a single `gws` invocation. It costs about 35 seconds and draws
nothing.

One caveat the split cannot see, recorded so nobody re-derives it: a 429 that
`gws` retries *inside* that one paginating call never reaches this code. A
`list` throttle in the counter is one that failed the whole invocation. What
the zero does establish is that listing never failed hard, and the timeline
puts every throttle well after listing ended.

**H4 - concurrency. Contributes, but is not the cause and does not set the
ceiling.** At a pinned 8 req/s, going from 4 to 8 workers raises throughput
5.16 to 5.54 msg/s and raises throttles 488 to 875. Going on to 12 workers
changes neither (5.57 msg/s, 868 throttles). Concurrency buys about 8%
throughput for 79% more rejected requests and then saturates. The observation
that the zero-throttle restore had *higher* concurrency than the throttled
fetch holds up: concurrency is not what separates them.

### The mechanism: a per-minute budget, spent by successes

The API says `Units per minute per user`, and the runs behave like a budget
rather than a rate limit:

1. **Every run starts clean and then falls off a cliff.** `r8c4` fetched its
   first 339 messages at 8.58 msg/s with zero throttles and hit the wall 40
   seconds in; `r16c4` got 293 messages and 20 seconds; `r5c4` got 733
   messages and 144 seconds. A rate ceiling would have throttled the first
   second. A budget drains first, and the higher the offered rate the sooner.
2. **The throttling then oscillates on a ~60 second period.** `r8c4` in 30s
   slices: 275 messages 0 throttles, then 91 and 78, then 174 and 17, then 120
   and 58, alternating for the rest of the run. That is the window rolling
   over. `r5c4`, under the budget, has no oscillation at all - a flat 150
   messages per slice for thirteen consecutive slices.
3. **Successes are conserved and attempts are not.** See the table. Whatever
   is metered is consumed by calls that return data; requests rejected for
   quota are evidently cheap or free, which is why a 60% increase in attempts
   yields no more messages.

The sustainable ceiling is **300 successful `messages.get` per minute**,
bracketed directly: `--rate 5` offers 300/min and draws 3 throttles in an
entire run; `--rate 6` offers 360/min, is clamped back to 314/min, and draws
251.

That number is not approximately right, it is exactly the published quota.
Excluding each run's opening clean phase, which spends a window it did not
fill, the seven steady-state rates are 286, 297, 299, 299, 302, 305 and 313
gets/min - a mean of **6,002 units/minute against a published budget of
6,000**, and every run inside 5%.

That resolves the original puzzle twice over, and both halves matter.

**The unit cost.** The two rows really were at the same *call* rate, 5.17/s,
and this document estimated both at "~1,550 units per minute at 5 units a
call". The fetch's half of that estimate was wrong by 4x:

| | calls/s | units/call | units/min | of the 6,000 budget |
|---|---|---|---|---|
| pinned `fetch` | 5.17 | **20** | 6,204 | **103%** |
| `untrash` restore | 5.17 | 5 | 1,551 | 26% |

One run was pressed exactly against the ceiling and the other was at a quarter
of it. There was never a paradox; there was a wrong constant.

**Offered versus achieved.** The comparison was also not like-for-like. The
fetch was *offering* 8 req/s and being cut back to 5.17, while the restore was
latency-bound at 1.55 s/call across 8 workers and could only *offer* 5.17. One
number is an outcome and the other an input, and they were put in the same
column.

### The ceiling above 8 req/s (TODO item 2)

Answered, and the answer is that there is nothing up there.

| pinned at | delivered | throttles | lost |
|---|---|---|---|
| 8  | 5.16 msg/s | 488 | 2 |
| 12 | 5.70 msg/s | 504 | 2 |
| 16 | 5.61 msg/s | 536 | 1 |

The 10% gain from 8 to 12 is real, but it is not headroom - it is recovery
speed, refilling faster in the seconds after a window rolls over. Note also
that `--rate 16` never offered 16: at concurrency 4 the clean-phase rate topped
out at 14.4 req/s, which is `concurrency / latency`, not the pin. Four workers
cannot offer more than about 14 req/s whatever the flag says.

The shipped default is `--rate 8 --concurrency 12`, which delivered 5.57 msg/s
- within 2.3% of the fastest configuration measured anywhere in this sweep.
**There is no case for raising `RATE_DEFAULT`, and none is proposed.**

### A proposal, not a change

The interesting direction turns out to be down, not up. Three things in the
data argue for it, and all three are the repo owner's call:

- **`--rate 5` delivers 88% of the fastest throughput ever measured for 0.6%
  of its throttles** - 5.00 msg/s against 5.70, three throttles against 504.
  Over a full 35,000-message inbox that is about 20 minutes slower.
- **Throttling is not free.** Every configuration at or above 6 req/s lost
  messages: one to three per 2,000, each after exhausting all twelve retries,
  each purely for quota. `--rate 5` lost none and cached 2,000 of 2,000. An
  undercounted sender is a ranking bug, not a slow scan, and `CLAUDE.md`
  already treats dropped messages as a correctness problem rather than a
  performance one.
- **If concurrency is tuned at all it should come down, not up.**
  `--rate 12 --concurrency 4` was the fastest run and drew 42% fewer throttles
  than the shipped default (504 against 868).

Both changes belong to item 4, which stays gated. What has changed is that the
mechanism is measured rather than guessed: a controller that paces against a
per-minute budget is a different algorithm from AIMD-on-rate, not a retune of
one, and this section is the specification it was waiting for. The minimal
version is not even adaptive - the budget appears to be a constant, and
`--rate 5` is already the controller.

### The number, read off Google's own table

The remaining question was why the ceiling sat at 10% of the 15,000
units/minute/user this document assumed. It does not. Both constants in that
sentence were wrong, and Google publishes the right ones:

- **The per-user budget is 6,000 quota units per minute**, not 15,000. Cloud
  projects created on or after **1 May 2026** are on the newer, smaller quota;
  projects that used the API between November 2025 and April 2026 kept the old
  one. This project is plainly on the new quota, since the old one would have
  permitted 750 messages/minute.
- **`messages.get` costs 20 units**, not 5.

`6000 / 20 = 300` messages per minute, or 5.00 msg/s. That is the ceiling, and
it is what all seven runs measured.

| call | units | ceiling at 6,000/min |
|---|---|---|
| `messages.list` | 5 | 1,200 pages/min |
| `messages.get` | **20** | 300 messages/min |
| `messages.trash` | **20** | 300 messages/min |
| `messages.untrash` | 5 | - |
| `messages.modify` | 5 | untrash + modify is 10 units, so 600 messages/min |

Three consequences worth carrying forward:

**A restore is four times cheaper per message than a scan, and a trash run is
exactly as expensive as one.** `trash --execute` has never been run at a rate
that would test this, but it is a `messages.get`-priced operation and the same
300/minute ceiling applies to it.

**The listing is 6% of one minute's budget** - 70 pages at 5 units. That is the
quantitative version of the H3 disproof.

**The keyring hypothesis is dead, and `DESIGN-UI.md`'s premise survives.** The
previous section speculated that a `get` might really cost 45-50 units because
`gws` reloads its keyring on every invocation, and that if the subprocess
overhead were *quota* rather than latency, the rejection of direct HTTPS was
taken on a false premise. It is not: 20 units is the published cost and the
measurements match it to within 5%, leaving nothing for a per-invocation
surcharge to explain. Subprocess cost is latency, exactly as
`DESIGN-UI.md` assumed. That decision stands on its original grounds.

Sources: Gmail API [usage limits](https://developers.google.com/workspace/gmail/api/reference/quota).

---

## The fix: pace the budget, 2026-09-09

The diagnosis above is the whole specification. The limiter paced **requests
per second** against a constraint denominated in **quota units per minute**,
where the price depends on the method - so one number was necessarily wrong
for at least one command, and it was:

| | offered | in units/min | against 6,000 |
|---|---|---|---|
| `fetch` at `--rate 8` | 8 get/s | 9,600 | **160%** |
| `untrash` at `--rate 8` | 8 mutate/s | 2,400 | 40% |

Simultaneously 60% too fast for the command that reads mail and 2.5x too slow
for the command that puts it back. That is not a tuning error, it is a units
error, and no constant could have fixed it.

### What changed

**One constant replaces the rate.** `UNITS_PER_MINUTE = 6000`, plus
`METHOD_UNITS` - Google's published table, not a tuning parameter. Every
command paces itself correctly from those: 300 messages/minute for a scan or a
trash, 1,200 calls/minute for an untrash or a modify.

**GCRA generalises rather than forks.** `reserve()` became `reserve(cost)`:
instead of one token per arrival, `cost` per arrival, so the interval is
`cost / rate` rather than `1 / rate`. In budget mode `rate` is units per
second and `cost` is the method's price; in `--rate` mode `rate` is requests
per second and `cost` is 1, which is byte-for-byte the old arithmetic. **One
limiter, three modes**, and every existing caller kept working unchanged
because `cost` defaults to 1.

**A retry pays again.** The API metered the attempt as surely as the fleet made
it, and charging only successes would let a run already over budget go further
over.

**Adaptation is gone from the default path.** The budget is a published
constant; there is nothing to search for. `--adaptive` still reaches the AIMD
controller and it is still broken. `--rate` still pins requests per second and
is still the escape hatch that produced the only usable run before any of this
was understood. `--budget` refuses to combine with either, because they are
different units and silently letting one win is how a run ends up paced by
something the operator did not choose.

### The simulation was the other half of the bug

`_simulate()` metered **arrivals over a trailing second**. Under that model a
rate cut immediately and proportionally reduces throttling, so AIMD converges
by construction - the headline test asserting ">20 msg/s sustained" was
asserting a property of the simulation. `CLAUDE.md` had said the quota was per
minute the entire time.

It now meters **units over a minute**, and rejected calls are charged nothing,
which is what the real runs showed: successes pinned near 300/min while
attempts ranged 300-479. Against that model:

| | sustained | throttled |
|---|---|---|
| budget 6,000 | 302 msg/min | 0.3% of attempts |
| pinned 8 req/s | 300 msg/min | **37.6% of attempts** |

Identical throughput; the old default simply wasted 38% of its requests. That
comparison is now `test_a_requests_per_second_pin_overruns_the_budget`, and it
fails if the default ever goes back to pinning req/s.

The ">20 msg/s" done-when from `DESIGN-UI.md` is retired. It was set before
anyone had read a throttle and it is not reachable on this API at any
concurrency, by any client: 6,000 units a minute divided by the 20 a
`messages.get` costs is 300 messages a minute. The replacement criterion is to
spend the whole budget and draw almost nothing.

### Measured against the real mailbox, same day

Three runs on one sender (953 messages, 557 of them in the inbox), which the
repo owner nominated as an expendable test case. No `--rate` and no
`--budget` on any of them; the `untrash` row is the one place a flag was
passed, `--concurrency 16`, and that turns out to matter - see the A/B below,
where the same restore at the default of 8 takes 180s instead of 344s.

| run | calls/message | messages | throughput | units/min | of budget | throttles | lost |
|---|---|---|---|---|---|---|---|
| `fetch` (get, 20u) | 1 | 900 | 5.02 msg/s | 6,024 | 100% | **0** | **0** |
| `fetch` (get, 20u) | 1 | 557 | 5.00 msg/s | 6,000 | 100% | **0** | **0** |
| `trash` (trash, 20u) | 1 | 557 | 5.00 msg/s | 6,004 | 100% | **1** | **0** |
| `untrash` (untrash+modify, 5u each) | 2 | 557 | 1.47 msg/s* | 884 | 15% | **0** | **0** |

*`--concurrency 16`. At the shipped default of 8 the same restore runs at 3.09
msg/s; the difference is the client, not the quota, and the next section is
about exactly that.

The first three sit exactly on the ceiling and draw essentially nothing, where
the same work under `--rate 8` drew 488 throttles and lost 2 messages per
2,000. The fourth is the point of the whole exercise: a cheap path is *not*
constrained to the expensive path's rate. It is latency-bound at 15% of budget,
and the limiter correctly stays out of its way.

All 557 were restored and confirmed in the mailbox: 557 back in the inbox, 0
left in Trash, checked by query rather than by trusting the tool's own count.
That run also exercised two things `TODO.md` listed as never having run for
real - `trash` on a batch whose manifest records labels, and `untrash --cache`
- and both were clean.

**One thing was unexplained**: the restore managed 1.47 msg/s at concurrency
16, where the 1,108-message restore earlier that day managed 2.58 msg/s at
concurrency 8. More workers, less throughput, no throttles and no drops either
time. The live line dipped to 0.9 msg/s for stretches, which is the signature
of `TRANSIENT` retries taking their local exponential backoff - and those
retries were invisible, counted only in a `server_errors` field no command
published. See the next section: the field was added, and it refuted this.

### The backoff hypothesis, measured and wrong

`server_errors`, a per-method split, sampled texts and a `backoff_seconds`
accumulator now reach the status file, and `_report_pacing()` is called by all
four long-running commands - `cmd_trash` and `cmd_untrash` had reported no
pacing at all, which is backwards, since the question came from a restore.

A controlled A/B on the same 557 messages, trashed and restored twice back to
back:

| leg | workers | elapsed | throughput | throttles | transient | backoff |
|---|---|---|---|---|---|---|
| `trash` | default 8 | 111.1s | 5.01 msg/s | 0 | 0 | 0.0s |
| `untrash` | **8** | **180.3s** | **3.09 msg/s** | 0 | 0 | 0.0s |
| `trash` | default 8 | 111.0s | 5.02 msg/s | 0 | 0 | 0.0s |
| `untrash` | **16** | **343.9s** | **1.62 msg/s** | 0 | 3 | **6.7s** |

The anomaly reproduces exactly - doubling the workers nearly halves the
throughput - and **backoff does not explain it**. 6.7 seconds of sleep against
a 163.6-second gap is 4%. The hypothesis this instrumentation was built to
confirm is refuted by the first run of it, which is the best thing a
measurement can do.

What the numbers do say is that **per-call latency rises with concurrency on
this client**:

| workers | calls/s | implied per-call latency |
|---|---|---|
| 8 | 6.18 | **1.29 s** |
| 16 | 3.24 | **4.94 s** |

Doubling the fleet made each call nearly four times slower, so total
throughput fell. Nothing on the Gmail side is implicated: zero throttles, zero
drops, 15% of the quota budget. This is the client saturating itself - the tool
spawns one `gws` process per API call, `gws` is Node and reloads its keyring on
every invocation, and sixteen of those starting concurrently is a different
kind of load than eight.

Two consequences:

**The shipped defaults are already on the right side of this.** `trash`,
`untrash` and `engaged` default to concurrency 8; only `fetch` uses 12, and a
fetch is quota-bound long before it is latency-bound. The 1.47 msg/s run that
raised the question was `--concurrency 16`, passed by hand. The tool was right
and the flag was wrong.

**The 16 hard maximum is about something else, and both limits stand.**
`CLAUDE.md` caps concurrency at 16 because above that the API silently drops
messages on `messages.get` - a correctness limit. This is a second, lower,
throughput ceiling on the mutation path, from an entirely different mechanism.
Raising mutation concurrency toward 16 is not dangerous, it is just slower.

The remaining unknown is now a narrow one: whether the per-call latency curve
is CPU, memory, or keyring contention. Measuring it needs process-level
instrumentation this tool has no business carrying, and it changes no default,
so it is recorded rather than pursued.
