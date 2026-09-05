# Design: local web UI

**Status: proposed, not implemented.** This document describes a planned
browser-based interface for the audit. Nothing here exists yet; the tool today
is CLI-only.

---

## Motivation

Three problems surfaced while running the audit against a real 35,000-message
inbox. None are cosmetic.

**The scan is invisible.** A full header fetch takes tens of minutes and prints
batch counters to a log. There is no way to tell whether it is progressing,
rate-limited, or wedged without inspecting file mtimes and process lists. On a
real run this led to repeatedly quoting an ETA that was wrong by a factor of
six.

**Approval happens in a text file.** The user reads a ranked table in the
terminal, then hand-writes sender addresses into `approved.txt`. It is
transcription work, it is easy to typo an address into a no-op, and it gives no
feedback about what the selection actually covers until the dry run.

**Throughput collapsed under sustained load.** Measured on a real inbox.
**Every figure below is pre-Phase-1** — they describe the per-request-backoff
behaviour that the shared limiter replaced, not current behaviour:

| Condition | Rate | Status |
|---|---|---|
| Short burst, concurrency 16 | 35.7 msg/s | historical |
| Short burst, concurrency 12 | ~26 msg/s (projected) | historical |
| **Sustained over 53 minutes, concurrency 12** | **5.1 msg/s** | **historical** |
| Sustained, post-limiter, concurrency 12 | *not yet measured* | — |

The burst benchmark measured a fresh quota bucket. Sustained, the per-minute
limit bound continuously and per-request exponential backoff — which climbs to
60 seconds — idled every worker independently. The tool spent most of its wall
clock asleep rather than near the quota ceiling.

The post-limiter row is deliberately empty. Fill it from one measured run
against a real mailbox; do not write in a projected number.

---

## Goals

- Make scan progress and rate-limit state legible while it runs.
- Replace `approved.txt` transcription with direct selection.
- Sustain throughput close to the actual quota ceiling instead of far below it.
- Make repeat runs cheap, so the tool is usable ongoing rather than once.
- Preserve every existing safety property without exception.

## Non-goals

- Hosting anything remotely. This is a localhost tool.
- Replacing the CLI. The UI is a front-end over the same functions; the CLI
  remains the scriptable path and the reference implementation.
- Adding runtime dependencies. Standard library only, as today.
- Fixing OAuth setup. That happens in Google's console before this tool runs.

---

## Architecture

`python gmail_audit.py ui` starts a `ThreadingHTTPServer`, opens a browser, and
serves a single-page app. The scan runs on a background thread. The page polls
a progress endpoint.

```
  browser (localhost only)
      |  JSON over HTTP, token-authenticated
      v
  ThreadingHTTPServer  ──►  scan thread  ──►  Gmail API
      |                          |
      |                          └──►  headers.jsonl  (existing cache)
      └──►  static SPA (one HTML file, inlined CSS/JS)
```

Reusing `headers.jsonl` means scans stay resumable and the CLI keeps working
unchanged against the same data. The UI adds a surface; it does not fork the
model.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/preflight` | auth state, mailbox totals, scope check |
| GET | `/api/progress` | fetched/total, observed rate, backoff state |
| POST | `/api/scan` | start or resume a scan |
| GET | `/api/senders` | ranked index with scores, signals, safeguards |
| POST | `/api/selection` | persist the approved set |
| POST | `/api/trash` | execute, token + explicit confirmation required |
| POST | `/api/untrash` | restore from manifest |

---

## Security model

A localhost server that can delete mail is a materially different risk profile
from a CLI. Three requirements, all mandatory before any mutating endpoint
exists.

### Bind to 127.0.0.1 explicitly

`http.server` binds `0.0.0.0` by default. Left alone, that exposes mail
deletion to every device on the network. Bind the loopback interface
explicitly and assert it in a test.

### Per-launch token on every mutating request

Any page open in the user's browser can issue requests to `localhost:8765`.
Without authentication, a hostile page could trigger deletions silently while
the user is on an unrelated site.

- Random token generated per launch, carried in the URL the tool opens
- Required on every state-changing request
- Never written to disk
- Validate `Origin` and `Host` headers to defeat DNS rebinding
- Emit no CORS headers

### Escape all header-derived text

**This is a new risk that the UI introduces and the CLI did not have.** The CLI
printed `Subject` to a terminal. A browser executes it. `Subject` is
attacker-controlled — a sender chooses its contents — so a crafted subject line
becomes stored XSS in a page that holds a token capable of deleting mail.

- All header-derived values rendered via `textContent`, never `innerHTML`
- No `dangerouslySetInnerHTML`-equivalent anywhere in the SPA
- A test asserting no template path emits raw header text

This is the same reasoning that already keeps `Subject` out of the scoring
function: it is data, from an untrusted party, and must never become code or
control flow.

---

## Performance

### Global rate limiter — the primary fix (shipped)

Per-request exponential backoff is gone, replaced by a **single token bucket
shared by all workers** (`RateLimiter`, `LIMITER`). Twelve workers used to
discover the limit independently and each sleep up to a minute; the shared
limiter keeps the fleet near the ceiling instead of oscillating between over it
and asleep.

It is **adaptive**: it ramps until 429s appear, backs off, and settles. Quota
varies by project, so a hardcoded constant would have been wrong for someone
else setting this up; `--max-rate` caps the search and `--rate` pins it
outright. `--concurrency` is now a parallelism knob covering latency, not the
de-facto rate control it used to be.

### Direct HTTPS instead of subprocess-per-message — rejected

Each header fetch spawns a `gws` process, costing roughly 200ms before any
network work. Calling the Gmail API directly with `urllib` would remove that
ceiling. **Phase 1 deliberately did not do this**, and the decision should not
be revisited casually:

- `gws auth export` would place a refresh token in this tool memory space,
  where no credential lives today.
- Routing around `gws` **bypasses `--sanitize` entirely**, since Model Armor is
  a `gws` service. That is a safety regression, and it was not obvious until
  the alternative was written down.

The measured outcome made the trade easy: the shared limiter reaches the
throughput target *through* the same subprocess path, because subprocess
overhead was never the binding constraint — uncoordinated sleeping was.

`gws` remains the authentication path — the tool should not reimplement OAuth.

### Incremental scans via the History API

After an initial full scan, use `users.history.list` with the stored
`historyId` to fetch only what changed. This turns an hour-long rescan into
seconds and is what makes the tool usable on an ongoing basis rather than as a
one-off.

---

## Screens

**1. Preflight.** Calls `getProfile` and reports auth state plainly. Catches
the scope trap — where `gws auth status` reports scopes the token does not
actually have — *before* the user waits an hour for a scan that cannot work.
Links to `SETUP.md` on failure.

**2. Scan.** Live progress, measured rate, ETA derived from observed
throughput rather than a benchmark, and explicit rate-limit state. A stall
should read as "waiting on quota, resuming in 12s", not as a frozen counter.

**3. Review.** The ranked table, sortable and filterable, with safeguard
badges. Checkboxes replace `approved.txt`. Bulk selection by predicate ("all
scoring ≥ 8 with no safeguard"). Per-sender expander showing message dates and
truncated subjects.

**4. Confirm.** Exact per-sender counts. Safeguarded senders listed separately
and requiring individual override — never swept along by a bulk select.

**5. Execute and undo.** Progress, then a persistent Undo backed by the
manifest.

---

## Safety invariants — unchanged

The UI inherits every existing guarantee, and none may be relaxed for
convenience:

- `messages.trash` only. No `delete` or `batchDelete` anywhere in the codebase.
- `gmail.modify` scope, so permanent deletion is impossible at the API level.
- Manifest written before any mutation, so an interrupted run still leaves a
  complete undo list.
- Replied-to, protected-domain and starred senders demote to Review. The UI
  must make overriding a safeguard a deliberate act, not a checkbox lost among
  two hundred others.
- `Subject` never contributes to a score.

On execute, still write `approved.txt` — an auditable record of the decision
that exists independently of the application's own state.

---

## Phasing

| Phase | Scope | Notes |
|---|---|---|
| 1 | Rate limiter | **Shipped.** Independent of the UI; fixed the pain being felt and speeds all later testing |
| 2 | Server, preflight, scan progress | No deletion path exists yet |
| 3 | Review table and selection | Replaces `approved.txt` |
| 4 | Execute and undo | Token and escaping must land *before* this |
| 5 | Incremental history scans | Makes ongoing use cheap |

The rate limiter led deliberately. It was the problem actually being felt, it
carried no UI risk, and every later phase is easier to test when a scan takes
minutes instead of an hour.

---

## Implementation notes

Read `CLAUDE.md` first — it carries the invariants, the offline test loop, and
the platform gotchas. Symbols below are in `gmail_audit.py` unless noted.

Both test suites run offline against fixtures with no Gmail access and no
quota cost. Develop against those; hitting a real mailbox to test a change
costs an hour and burns quota you will then be rate-limited by.

### Phase 1 — shared rate limiter (shipped)

**Touched:** `gws()` (the old `RETRYABLE` retry loop), `cmd_fetch()`,
`cmd_engaged()`, `_safe()`. `PLAN-RATE-LIMITER.md` carries the full rationale.

Each worker used to discover the quota ceiling independently and sleep up to
60s alone. That is now one token bucket shared across the pool: `RateLimiter`
in GCRA form, so a caller claims a departure deadline under the lock and then
sleeps alone until its own private instant. Token accrual is a pure function of
wall time, so no waiter ever wakes to find its slot taken and no thundering
herd can form. It sizes itself by AIMD — ramp until 429s appear, back off,
settle.

The conflated `RETRYABLE` regex split into `THROTTLE` and `TRANSIENT`, because
the two failure modes call for opposite responses. A throttle means the *fleet*
is too fast: shrink the shared rate, re-queue, take no local sleep. A 5xx means
*this request* failed: keep the per-request backoff and leave the rate alone.

Three guards keep the sawtooth bounded — only raise the rate when the limiter
is actually binding, hold ~15s after any decrease, and coalesce throttles
inside a ~2s window into a single decrease. The last is what stops one worker
429 from tanking the fleet; twelve compounding decreases would give `0.7^12`.

Drops are counted, summarised and written to `fetch-dropped.jsonl` for retry
rather than scrolling past as stderr noise, and a consecutive-failure circuit
breaker converts a mid-run token expiry into one clear line instead of 30,000.

**Done when:** a 5,000-message fetch sustains >20 msg/s end-to-end with zero
dropped messages, and the fetch reports its observed rate. A run that is fast
but drops messages is a failure, not a partial success — dropped messages
silently undercount senders.

**Status:** the offline discrete-event simulation settles at 0.75–0.99x a
simulated ceiling and clears the >20 msg/s bar at ceiling 35
(`test_fleet_clears_the_twenty_messages_per_second_bar`). The real-mailbox
measurement that fills the empty table row above has not been run yet.

### Phase 2 — server, preflight, scan progress

**Touch:** new `cmd_ui()`; reuse `list_ids()`, `cmd_fetch()`, `load_cache()`.

`ThreadingHTTPServer` bound to `127.0.0.1` explicitly. Scan on a background
thread. No mutating endpoint exists in this phase.

**Done when:** the browser shows live progress, the observed rate, and
rate-limit state; preflight correctly distinguishes "not authenticated" from
"authenticated but missing the Gmail scope" — the 403 case that `gws auth
status` misreports.

### Phase 3 — review and selection

**Touch:** `score_sender()`, `cmd_rank()` refactored to return rows rather than
print them, so CLI and UI share one ranking path.

**Done when:** selection in the UI produces exactly the set `cmd_trash` would
act on given the equivalent `approved.txt`, verified by a test comparing both
paths against the fixture.

### Phase 4 — execute and undo

**Touch:** `cmd_trash()`, `cmd_untrash()`, `_trash_one()`.

The token check and output escaping from the security section must land
**before** this phase adds any mutating endpoint. Not alongside it.

**Done when:** a trash run through the UI writes the same manifest the CLI
writes, Undo restores from it, and tests assert that a request without a valid
token, or with a foreign `Origin`, is rejected.

### Phase 5 — incremental scans

**Touch:** new history-based fetch path; `getProfile` already returns
`historyId`.

**Done when:** a rescan after an initial full scan completes in seconds and
finds new mail, with a documented fallback to a full scan when the stored
`historyId` is too old — Gmail expires them.

### Keeping this document honest

The performance figures here are measurements from a specific run, not
permanent properties. Phase 1 has landed, so "5.1 msg/s sustained" is now
marked historical in the table above. The post-limiter row stays empty until a
real run fills it — an unmeasured projection sitting in that column would be
exactly the thing this section exists to prevent.

## Risks and open questions

**Handling the refresh token — decided: no direct HTTPS.** Direct HTTPS calls
would require the tool to obtain an access token from the credentials `gws`
stores. Phase 1 kept subprocess-per-message instead, so credentials stay
entirely inside `gws` — and, just as importantly, `--sanitize` keeps working,
since Model Armor is a `gws` service. The throughput cost turned out not to
exist: the shared limiter hits the target through the subprocess path, because
uncoordinated sleeping, not process spawn cost, was the binding constraint.

**Quota ceilings vary.** The adaptive limiter must not assume the ceiling
observed on one project applies to another.

**A UI invites bulk mistakes.** Selecting two hundred senders is much easier
than typing two hundred lines into a text file. The friction being removed is
partly protective friction. This is why safeguard overrides must stay
individual, and why Undo must be prominent rather than buried.

**Browser as an attack surface.** The CLI rendered untrusted text to a
terminal. The UI renders it in a JavaScript context holding a deletion
capability. This is the single largest new risk in the design and the reason
the escaping requirement is non-negotiable.
