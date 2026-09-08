# Design: local web UI

**Status: phases 1, 2 and the goal of 3 have shipped; 5 is next and 4 is
deferred until the tool's shape settles.** The rate limiter, the localhost server
and the review file all exist. Selection no longer requires transcription, but
it happens in a file rather than in the browser - see **Phase 3, reassessed**
below for why that turned out to be the better artifact, and what it does to
the phases after it.

---

## Motivation

Three problems surfaced while running the audit against a real 35,000-message
inbox. None are cosmetic.

**The scan is invisible.** *Fixed.* A full header fetch takes tens of minutes
and printed batch counters to a log. There was no way to tell whether it was
progressing, rate-limited, or wedged without inspecting file mtimes and process
lists. On a real run this led to repeatedly quoting an ETA that was wrong by a
factor of six.

A second half of this only surfaced later: the scan also **could not be walked
away from**. It lived in a foreground process, published nothing, and died with
its shell, so "how far along is the run in that other window?" had no answer at
all - and the UI could only report on scans it had started itself.
`StatusWriter` publishes the same snapshot to a file on the reporter's existing
tick; `gmail_audit.py status` and the UI both read it.

**Approval happens in a text file.** *Fixed, but not the way this said.* The
user read a ranked table in the terminal, then hand-wrote sender addresses into
`approved.txt`. It was transcription work, it was easy to typo an address into
a no-op, and it gave no feedback about what the selection covered until the dry
run.

The diagnosis above was off by one, and it is worth recording why, because it
nearly bought an SPA to solve it. Being a text file was never the problem. The
problem was that the ranked **output** and the approval **input** were two
different artifacts, so a human transcribed between them. The file was also, at
the same time, one of the tool's better safety properties: an auditable record
of the decision that exists independently of any application state, which is
why the security section below insists on still writing one.

Making the ranked index and the approval list the same file removes the
transcription without giving up the artifact. `rank --review` writes it,
`trash --review` reads it, and a decision costs one character.

**Throughput collapsed under sustained load.** Measured on a real inbox.
**Every figure below is pre-Phase-1** — they describe the per-request-backoff
behaviour that the shared limiter replaced, not current behaviour:

| Condition | Rate | Status |
|---|---|---|
| Short burst, concurrency 16 | 35.7 msg/s | historical |
| Short burst, concurrency 12 | ~26 msg/s (projected) | historical |
| **Sustained over 53 minutes, concurrency 12** | **5.1 msg/s** | **historical** |
| Sustained, adaptive limiter, concurrency 12 | **3.7 msg/s** | measured 2026-09-08 |
| Sustained, adaptive limiter, concurrency 4 | **3.9 msg/s** | measured 2026-09-08 |
| **Sustained, limiter pinned `--rate 8`, concurrency 4** | **5.2 msg/s** | measured 2026-09-08 |

The burst benchmark measured a fresh quota bucket. Sustained, the per-minute
limit bound continuously and per-request exponential backoff — which climbs to
60 seconds — idled every worker independently. The tool spent most of its wall
clock asleep rather than near the quota ceiling.

The row was deliberately left empty for months. It is now filled, and the
answer is bad: **the adaptive limiter is slower than the pathology it
replaced**, and slower still than simply pinning the rate. It also collapses
to `RATE_MIN` on every adaptive run. `docs/PLAN-RATE-LIMITER.md` carries the
full measurement, the mechanism, and why the offline simulation could not have
caught it.

Drops were rare but not zero: 6 messages across roughly 14,000 attempts in four
runs. Two of them are what finally revealed the throttle's actual text, which
names a **per-minute** budget — the constraint `CLAUDE.md` always described and
the offline simulation never modelled.

---

## Goals

- Make scan progress and rate-limit state legible while it runs. *Shipped.*
- Make a running scan legible from outside the process that started it, so it
  can be walked away from. *Shipped; added after the fact.*
- Remove the `approved.txt` transcription without removing the artifact.
  *Shipped, as the review file rather than as a table.*
- Sustain throughput close to the actual quota ceiling instead of far below it.
  *Shipped in simulation; unmeasured against a real mailbox.*
- Make repeat runs cheap, so the tool is usable ongoing rather than once.
  *Partly shipped, and the phase-5 half was aimed at the wrong cadence. See
  the note under phase 5.*
- Preserve every existing safety property without exception. *Held.*

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
a progress endpoint. **Shipped in phase 2**, minus the endpoints marked below.

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

| Method | Path | Purpose | Status |
|---|---|---|---|
| GET | `/api/preflight` | auth state, mailbox totals, scope check | shipped |
| GET | `/api/progress` | fetched/total, observed rate, backoff state | shipped |
| POST | `/api/scan` | start or resume a scan | shipped |
| GET | `/api/senders` | ranked index with scores, signals, safeguards | phase 3, optional |
| POST | `/api/selection` | persist the approved set | superseded by the review file |
| POST | `/api/trash` | execute, token + explicit confirmation required | phase 4, deferred |
| POST | `/api/untrash` | restore from manifest | phase 4, deferred |

`test_ui_exposes_no_mutating_route` asserts the bottom four are absent. A phase
that adds one is expected to update that test deliberately, which is the
point: the route table cannot grow a deletion path by accident.

`/api/selection` is struck rather than deferred. Persisting the approved set
through an HTTP endpoint into application state was always a worse artifact
than a file the user can read, diff and keep; the review file is that file.

---

## Security model

A localhost server that can delete mail is a materially different risk profile
from a CLI. Three requirements, all mandatory before any mutating endpoint
exists.

**All three shipped in phase 2**, guarding a surface that cannot yet delete
anything. That ordering was deliberate: landing them alongside the deletion
path would have meant the first version of that path was the one being tested.

### Bind to 127.0.0.1 explicitly

`http.server` binds `0.0.0.0` by default. Left alone, that exposes mail
deletion to every device on the network. Bind the loopback interface
explicitly and assert it in a test.

Shipped as `_ui_bind_address()`, which *raises* on anything but loopback, and
there is no `--host` flag to reach it with. Absent capability beats remembered
intent. `test_ui_binds_loopback_only` covers both halves.

### Per-launch token on every mutating request

Any page open in the user's browser can issue requests to `localhost:8765`.
Without authentication, a hostile page could trigger deletions silently while
the user is on an unrelated site.

- Random token generated per launch, carried in the URL the tool opens
- Required on every request, not only the state-changing ones: a GET that
  triggers a real API call spends quota, and the page itself is token-gated so
  a stale bookmark cannot reach it
- Never written to disk
- Validate `Origin` and `Host` headers to defeat DNS rebinding
- Emit no CORS headers

Also shipped: a `Content-Security-Policy` of `default-src 'none'` with
`connect-src 'self'`. The page loads nothing from anywhere - no CDN, no font,
no analytics - so an injected `<script>` in a later phase would have no channel
to send anything out. `test_ui_page_never_writes_markup` asserts the page
contains no external URL at all, which is what keeps that policy true.

### Escape all header-derived text

**This is a new risk that the UI introduces and the CLI did not have.** The CLI
printed `Subject` to a terminal. A browser executes it. `Subject` is
attacker-controlled — a sender chooses its contents — so a crafted subject line
becomes stored XSS in a page that holds a token capable of deleting mail.

- All header-derived values rendered via `textContent`, never as markup
- No `dangerouslySetInnerHTML`-equivalent anywhere in the SPA
- A test asserting no template path emits raw header text

Shipped: the page has exactly one text-setting helper, and
`test_ui_page_never_writes_markup` fails on any markup-writing API appearing in
`UI_HTML`. It bans the literal strings, so the test caught them in this
document's own implementation comments while phase 2 was being written - which
is roughly the level of paranoia intended.

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

**1. Preflight.** *Shipped.* Calls `getProfile` and reports auth state plainly.
Catches the scope trap — where `gws auth status` reports scopes the token does
not actually have — *before* the user waits an hour for a scan that cannot
work. Points at the relevant `SETUP.md` section on failure.

**2. Scan.** *Shipped.* Live progress, measured rate, ETA derived from observed
throughput rather than a benchmark, and explicit rate-limit state. A stall
reads as `backoff 12s`, not as a frozen counter — and the minutes spent inside
`list_ids()` before any counter exists read as "enumerating message IDs", which
is the other way a working scan can look wedged.

**3. Review.** *Phase 3b, optional - the review file covers the goal already.*
The ranked table, sortable and filterable, with safeguard
badges. Checkboxes replace `approved.txt`. Bulk selection by predicate ("all
scoring ≥ 8 with no safeguard"). Per-sender expander showing message dates and
truncated subjects.

**4. Confirm.** *Shipped in the CLI; deferred in the browser.* Exact
per-sender counts. Safeguarded senders listed separately
and requiring individual override — never swept along by a bulk select.

**5. Execute and undo.** *Deferred; see phase 4.* Progress, then a
persistent Undo backed by the manifest.

---

## Safety invariants — unchanged

The UI inherits every existing guarantee, and none may be relaxed for
convenience:

- `messages.trash` only. No `delete` or `batchDelete` anywhere in the codebase.
- `gmail.modify` scope, so permanent deletion is impossible at the API level.
- Manifest written before any mutation, so an interrupted run still leaves a
  complete undo list.
- Replied-to, protected-domain, starred and mostly-important senders demote to
  Review (`--important-guard` sets how the last one behaves). The UI
  must make overriding a safeguard a deliberate act, not a checkbox lost among
  two hundred others.
- `Subject` never contributes to a score.

On execute, still write an auditable record of the decision that exists
independently of the application's own state. The review file **is** that
record now: it is the input, so it cannot drift from what was actually done.

---

## Phasing

| Phase | Scope | Notes |
|---|---|---|
| 1 | Rate limiter | **Shipped.** Independent of the UI; fixed the pain being felt and speeds all later testing |
| 2 | Server, preflight, scan progress | **Shipped.** No deletion path exists; the token, the bind and the escaping landed here |
| 2.5 | Status file, `status` subcommand | **Shipped.** Not originally a phase. A scan you cannot walk away from is barely usable, and the UI could only see its own scans |
| 3 | Selection without transcription | **Shipped as a file, not a table.** `rank --review` / `trash --review`. See below |
| 3b | Review table in the browser | Optional. A better *view* over the review file; no longer on the critical path |
| 4 | Execute and undo in the browser | **Deferred.** Not blocked and not declined: it is three layers deep, and the selection model has already moved once. Decide it with 3b |
| 5 | Incremental history scans | **Downgraded.** `historyId` expires in about a week, so it cannot serve an annual cadence. The resumable caches already deliver most of it. See below |

The rate limiter led deliberately. It was the problem actually being felt, it
carried no UI risk, and every later phase is easier to test when a scan takes
minutes instead of an hour.

Phase 2 followed the same logic one level up: the security machinery a
deletion endpoint needs is easier to get right, and much easier to test, on a
surface that cannot delete anything if it is wrong.

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

**Status: done-when NOT met.** The offline simulation settles at 0.75–0.99x a
simulated ceiling and clears the >20 msg/s bar at ceiling 35
(`test_fleet_clears_the_twenty_messages_per_second_bar`), but that test asserts
a property of the simulation rather than of the limiter: `_simulate()` models
the quota as a trailing **one-second** window, and `CLAUDE.md` states plainly
that Gmail's is **per minute**. Under a one-second model a rate cut reduces
throttling proportionally, so AIMD converges by construction.

Measured against a real mailbox on 2026-09-08: **3.7 msg/s sustained at
concurrency 12**, against a bar of 20, with the limiter collapsing from 3.63 to
1.00 req/s (`FLOOR`). Pinning the rate with `--rate 8` gave 5.2 msg/s, stable —
so the adaptive controller is a net loss. Six messages were dropped across the
four runs.

See `docs/PLAN-RATE-LIMITER.md`, "Measured against a real mailbox", for the
mechanism: the decrease gate is 2s and the increase gate is 15s, so whenever
throttles arrive more often than every 15 seconds the increase branch is
unreachable and the rate can only fall.

### Phase 2 — server, preflight, scan progress (shipped)

**Touched:** new `cmd_ui()`, `make_ui_server()`, `_UIHandler`, `ScanState`,
`preflight()`, `classify_gws_error()`, `UI_HTML`. Nothing existing changed:
the UI calls `cmd_fetch()` and `load_cache()` as they already were.

`ThreadingHTTPServer` bound to `127.0.0.1` explicitly, on a background thread
per request; the scan itself on one more. No mutating endpoint exists.

The scan path is `cmd_fetch()` itself, not a reimplementation of it. That is
what keeps the cache, the resumability, the drop accounting, the circuit
breaker and the reconciliation identical between the two front ends, and
`test_ui_scan_runs_the_very_same_fetch_path` asserts it against the same
fixtures the CLI tests use. `_ui_run_scan()` exists only to catch the
`sys.exit()` that `cmd_fetch` uses to report an abort — in a thread that is a
silently swallowed `SystemExit`, and the message it carries is exactly what
the page needs to show.

Preflight is a real `getProfile` call with deliberately shallow retries: it
answers a question, and a user staring at a blank panel should not wait out
six backoffs to learn they are not logged in. `classify_gws_error()` is a pure
function over the literal error strings indexed in `SETUP.md`, so the mapping
is testable offline and a wording change in `gws` degrades to "here is the raw
message" rather than to a confident wrong instruction.

**Done when:** the browser shows live progress, the observed rate, and
rate-limit state; preflight correctly distinguishes "not authenticated" from
"authenticated but missing the Gmail scope" — the 403 case that `gws auth
status` misreports. **Met**, against a stubbed transport and checked in a
browser; the 18 tests named `test_ui_*` and `test_preflight_*` cover the
guards, the classification and the scan path.

**Not done in this phase, deliberately:** the page does not render a single
mailbox-derived string yet — it shows counters and the user's own address.
Phase 3 is where sender and `Subject` text first reaches a browser, which is
why the escaping discipline had to be in place before it, not with it.

### Phase 2.5 — the status file (shipped, not originally planned)

**Touched:** `StatusWriter`, `read_status()`, `cmd_status()`, and one call
inside `_progress_reporter()`.

A one-hour scan that dies with its shell and publishes nothing is barely
usable, whatever it prints while you watch it. The reporter thread already
computes a snapshot every two seconds; it now writes that snapshot to
`fetch-status.json` as well. `gmail_audit.py status` reads it, and so does the
UI, which is what stops the page reporting "idle" over a fetch running in a
terminal.

Liveness is judged by the file's **mtime**, never by probing the pid.
`os.kill(pid, 0)` is the usual idiom and is a trap here: on Windows `os.kill`
ignores the signal for anything but `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` and calls
`TerminateProcess`, so the line asking whether the scan is alive would kill it.
`test_status_never_probes_the_pid` walks the parsed tree for any `kill` call.

**Done when:** a scan started in one terminal is legible from another, and a
killed scan reads as stopped rather than as permanently running. **Met.**

### Phase 3, reassessed — selection without transcription (shipped as a file)

The original plan was a checkbox table in the browser. What shipped is a file,
and the difference is worth writing down because the reasoning generalises.

**Touched:** `rank_rows()`, `sender_guard()`, `group_by_sender()`,
`load_engaged()` extracted from `cmd_rank()`; new `write_review()`,
`parse_review()`, `load_review_approved()`; `cmd_trash()` gained `--review`.

The `cmd_rank()` refactor this phase always needed - return rows rather than
print them - happened anyway, and the table, the JSON and the review file are
now three renderings of one ranking rather than three rankings kept in
agreement by hand. The test helper that used to reimplement scoring and
safeguards now calls `rank_rows()` too, so the safeguard tests can no longer
pass against a copy while the real ranking regresses.

**Done when:** selection produces exactly the set `cmd_trash` would act on
given the equivalent `approved.txt`, verified by a test comparing both paths
against the fixture. **Met** by
`test_review_and_senders_file_produce_the_same_targets`, which is the original
criterion with the word "UI" removed.

Three properties the file has that the table would have had to re-earn:

- It works over SSH, in `vim`, in a diff, and in a git history.
- It is the audit trail. The security section already required writing one; it
  is now the same object as the input, so it cannot drift from what was done.
- It survives being half-finished. `write_review()` merges by default and there
  is no overwrite flag, so reviewing across several sittings - or fetching more
  mail part way through - never discards a decision.

And two safety properties that had to be designed for, not inherited:

- **Every row is written unmarked.** The doc's own risk section says the
  friction being removed is partly protective, and it is right. A file that
  arrives pre-marked on two hundred senders and needs only a save is *more*
  dangerous than typing two hundred lines. `--preselect-score` exists, it is
  off by default, and it will not mark a safeguarded sender at any threshold.
- **`cmd_trash` recomputes the safeguards** from the cache instead of reading
  the `[!]` flag off the file, so deleting the flag by hand removes the marker
  and not the warning. Overriding one then takes a second deliberate act: the
  run stops and asks you to type `override`.

One failure mode the file does *not* fix, and which is worth being honest
about: a transposed domain is still a syntactically valid address, so no parser
can tell `news@deals.exmaple.com` from a real sender. The file closes it a
different way - it is generated from the cache, so a marked sender that matches
nothing is a contradiction rather than a plausible line, and `--review` treats
it as an error instead of quietly doing less than asked.

### Phase 3b — the review table in the browser (optional)

What the file genuinely cannot do: a per-sender expander showing message dates
and truncated subjects, so you can see *why* a sender scored 8 before deciding;
sorting and filtering; a live count of what the current selection covers.

Those are real, and they are the whole remaining case for the table. Build it
as a **view over the review file** - read it, write it back - not as a second
selection mechanism with its own state. This is where sender-chosen text and
`Subject` first reach a browser, so the escaping discipline from phase 2 starts
being load-bearing rather than precautionary.

### Phase 4 — execute and undo in the browser (deferred)

**Touch:** `cmd_trash()`, `cmd_untrash()`, `_trash_one()`.

**Deferred, not declined.** Nothing blocks it: the token check and the output
escaping landed in phase 2, as required. It is deferred because it is not worth
building against a tool whose shape is still moving.

Its original case was that selection happened in the browser, so execution
should finish there rather than sending the user back to a terminal, and that
Undo needed to be prominent rather than buried. Selection no longer happens in
the browser, and `untrash --manifest` already exists, so the first half of that
case is gone and the second is partly covered - `cmd_trash` prints the exact
undo command when it finishes.

**The reason to wait is iteration cost, not risk.** A browser execute path is
three layers deep: `cmd_trash`, an endpoint, and a page. Every change to the
selection model or the confirm semantics has to be made in all three. Phase 3
already moved once, from a table to a file, and the review file has not yet
been used against a real mailbox even once. Building the UI after the model
settles costs no more than building it now and should need far fewer
full-stack passes.

**Two arguments recorded honestly, because the case is not one-sided.**

*Against building it, weaker than an earlier draft of this section claimed.*
That draft called the mutating endpoint "the single largest new risk in this
design" and left it there. That phrase was written about a different tier of
risk. The capability in question is `messages.trash`, on a tool that writes a
complete manifest before mutating and holds a scope under which permanent
deletion is impossible. The worst case is mail in Trash with an undo list on
disk and thirty days to use it: bad, not catastrophic. Compare the absences
that phrasing was built for - no `messages.delete` in the source, `gmail.modify`
rather than `mail.google.com/` - which prevent *irreversible* loss. Same shape,
different stakes. Do not reuse the stronger wording for the weaker case.

*For building it, underweighted in that same draft.* A confirm screen with
exact per-sender counts and safeguarded senders listed separately reads better
than terminal output that scrolls, and a visible Undo button beats recalling a
command for someone who has just trashed eight thousand messages and is
alarmed. That is precisely the moment interface quality matters most.

**Decide it together with phase 3b, not separately.** Building 3b without 4
produces the exact seam the original design set out to avoid: select in the
browser, then switch to a terminal to execute. The coherent options are
*neither* - the browser stays read-only and the review file remains the
decision surface - or *both*, with the escaping discipline and the mutating
endpoint arriving together as originally planned. "3b but not 4" is the one
combination that makes no sense.

**Done when (if built):** a trash run through the UI writes the same manifest
the CLI writes, Undo restores from it, and tests assert that a request without
a valid token, or with a foreign `Origin`, is rejected.

### Phase 5 — incremental scans

**Touch:** new history-based fetch path; `getProfile` already returns
`historyId`.

**Done when:** a rescan after an initial full scan completes in seconds and
finds new mail, with a documented fallback to a full scan when the stored
`historyId` is too old — Gmail expires them.

**Downgraded from "the highest-value remaining item", and the reason is in that
done-when.** Gmail expires a `historyId` after roughly a week, sometimes hours.
This tool's cadence is annual. Every real run would therefore land on the
documented fallback, which is a full scan: phase 5 would add a history path,
a stored cursor, an expiry check and a fallback, and then take the fallback
every single time. It is the right feature for a daily agent and the wrong one
for a once-a-year audit.

Two things already deliver most of what this phase was for, and they were built
for other reasons:

- **`headers.jsonl` is resumable and keyed by message ID.** A second `fetch`
  enumerates IDs and fetches only what it has not seen. The re-enumeration is
  the cost, not the re-fetch.
- **`engaged-cache.jsonl` does the same for the sent-mail scan**, which was the
  longer of the two.

So the honest version of "make repeat runs cheap" is now: the expensive part of
a repeat run is enumerating IDs, and the fix for *that* is a faster or
narrower enumeration, not a history cursor. `--query` already narrows it by
hand.

Build this if the tool ever grows a daily or weekly mode. Until then it is
open, not next.

### Keeping this document honest

The performance figures here are measurements from a specific run, not
permanent properties. Phase 1 has landed, so "5.1 msg/s sustained" is now
marked historical in the table above. The post-limiter row stays empty until a
real run fills it — an unmeasured projection sitting in that column would be
exactly the thing this section exists to prevent.

**It is still empty after phase 2.** Phase 1's own done-when has two halves,
and only one of them is met: the offline simulation clears the 20 msg/s bar,
but no run against a real mailbox has been measured. Phase 2 makes that
measurement easy — the observed rate is now on screen while the scan runs, so
filling the row is a matter of reading it off one real scan — but easy is not
the same as done. Do not mark phase 1 fully complete, or fill that row, from
the simulation.

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

*Partly answered.* The review file removes the transcription without removing
the decision: every row arrives unmarked, `--preselect-score` is opt-in and
cannot touch a safeguarded sender, and an override costs a second deliberate
act at execute time. The general lesson is that the friction worth keeping is
the friction of **deciding**, not the friction of **typing**, and the two are
easy to conflate when the same file carries both.

**Browser as an attack surface.** The CLI rendered untrusted text to a
terminal. The UI renders it in a JavaScript context holding a deletion
capability. This is the single largest new risk in the design and the reason
the escaping requirement is non-negotiable.
