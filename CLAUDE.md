# CLAUDE.md

Context for working on this repository.

## What this is

A CLI that audits a Gmail inbox by **message headers only**, ranks senders by
spam signals, and bulk-trashes senders the user has explicitly approved. Python
3.8+, **standard library only** — no dependencies, no virtualenv. It shells out
to the `gws` CLI for Gmail API access.

Everything lives in one module, `gmail_audit.py`, with subcommands: `baseline`,
`doctor`, `fetch`, `engaged`, `rank`, `status`, `trash`, `untrash`, `ui`. The `ui`
subcommand serves a localhost page with an auth preflight and live scan
progress; it is phase 2 of `docs/DESIGN-UI.md` and has no mutating endpoint.
`status` reports on a scan running in another terminal by reading the file the
scan publishes.

## Hard rules

These are safety properties, not preferences. Do not relax any of them for
convenience, and do not assume a request to "clean up" or "simplify" includes
them.

**No permanent deletion, ever.** The only mutating API calls are
`messages.trash` and `messages.untrash`. `messages.delete` and
`messages.batchDelete` must never appear in this codebase. They require the
`https://mail.google.com/` scope; the tool authenticates with `gmail.modify`,
under which Google itself refuses permanent deletion. `test_no_permanent_delete_code_path`
greps the source and fails if either appears. If that test is in your way, you
are doing something wrong.

**`Subject` never influences classification.** It is a header, but it is
attacker-controlled free text — the sender chooses it. It is collected for
clustering and the audit trail only. `test_subject_never_contributes_to_score`
injects an adversarial subject and asserts the score is unchanged. Anywhere
`Subject` is displayed it must be truncated, and in any HTML context it must be
escaped as text, never markup.

**Headers only, never bodies.** All reads use `format=metadata` with an
explicit `metadataHeaders` allowlist, under which the Gmail API returns no body
payload at all. Do not add `format=full` or `format=raw`.

**The replied-to safeguard is required, not advisory.** `rank --review` and
`cmd_trash` both call `require_engaged()` and *exit* when `engaged.txt` is
missing. A warning was not enough: without that list the output looks correct
either way, senders you correspond with get marked trashable, and the
SAFEGUARD OVERRIDE block prints a confident, incomplete answer because it can
still see protected domains and stars. An existing but *empty* file is a real
answer (a mailbox with no sent mail) and passes; only a missing file refuses.
`--allow-missing-engaged` is the explicit override. The plain `rank` table
keeps the old warning, because it is informational and does not become a
trash list.

**An interrupted or aborted `engaged` scan writes no `engaged.txt`.**
`require_engaged()` only checks that the file *exists*, so a partial safeguard
list is indistinguishable from a complete one: it would pass the guard while
covering a fraction of the people you write to. The checkpoint
(`engaged-cache.jsonl`, one record per scanned message) is what survives an
interruption; the artifact is written only when the scan completes. Do not
"helpfully" write what was collected so far.

**Approval is a list, not a threshold.** `cmd_trash` refuses to run without an
explicit file of approved sender addresses. It must never act on "everything
scoring above N".

**The review file arrives unmarked.** `write_review` writes every row with `.`.
`--preselect-score` is opt-in, and it must never mark a safeguarded sender at
any threshold. The friction being removed is transcription, not judgement: a
file that arrives pre-marked on two hundred senders and needs only a save is
more dangerous than typing two hundred lines. Two tests cover this. Do not add
a "mark everything recommended" default.

**A filtered review file is a view, never a rewrite.** `--min-score N` hides
rows below `N` and hides only *undecided* ones: a sender already marked in the
file is written whatever they score. Without that rule, narrowing the file
would silently discard decisions already made, and the file is the only record
of them - the same failure the no-overwrite rule above exists to prevent,
arriving through a different door. The filter runs *before* preselect, so
`--preselect-score` can never mark a row the operator was not shown. A filtered
file says `FILTERED` in its header, because a truncated review file that does
not say it is truncated is indistinguishable from a complete one and the
difference is thousands of senders. `hidden` and `dropped` are reported
separately and must stay that way: hidden means one flag away from coming
back, dropped means gone from the cache. Four tests cover this, including the
round trip back out to the full file.

**Safeguards are recomputed, never read off the file.** `cmd_trash` calls
`sender_guard()` against the cache, so deleting a `[!]` by hand removes the
marker and not the warning, and overriding one still costs a typed `override`
at execute time. Do not "simplify" this into trusting the flag in the file.

**A marked sender that matches nothing is an error, not a skip.** The review
file is generated from the cache, so a `t` on a sender with no cached messages
was typed by hand. A transposed domain is still a syntactically valid address,
so no parser catches it; refusing the run is what stops it being silent. The
plain `--senders` path keeps the softer note, because there the list is
hand-written by design.

**Manifest before mutation.** `cmd_trash` writes every target message ID to
disk *before* trashing anything, so an interrupted run still leaves a complete
undo list for `cmd_untrash`.

**Safeguards demote, never promote, and apply only at the Trash boundary.**
Senders that are replied-to, on a protected domain, starred, or
mostly-important are demoted from `Trash` to `Review`. They constrain the
*ranking*; they deliberately do not override a human's approved list.

`rank_rows` checks the score first and the guard second, and that order is the
rule, not a detail. Checking the guard first - as it did until this was
measured - moved every guarded sender to `Review` from wherever they started,
including from `Keep`, which is a promotion. A guard exists to stop a `Trash`
recommendation, and a sender scoring below 6 was never going to get one, so
the move changed no outcome and only lengthened the list a human reads: on the
first real mailbox, 1,303 senders scoring under 3, or 38% of the review pile,
none of them at any risk. A guarded `Keep` still carries `[!]` and is still
refused by `--preselect-score`; it is simply not called out for review. Three
tests cover this, one of them asserting the guard still holds at the boundary
in all four flavours.

**`STARRED` and `IMPORTANT` are not the same evidence, and are not aggregated
the same way.** A star is a decision the user made and is rare, so `any` across
a sender's history is the right question. `IMPORTANT` is applied automatically
by Gmail and is common, so `any` across a hundred messages asks how many
messages the sender sent, not whether they matter — for a well-calibrated label
flagging 15% of mail, a 100-message sender is immune with probability ~1. It
was measured: on the first real mailbox (5,192 senders, 34,953 messages) the
`any` rule immunised **100%** of senders with 100+ messages and left 0.5% of
the inbox trashable. `--important-guard` (`off` | `majority` | `any`, default
`majority`, on both `rank` and `trash`) is the control; `important_guards()`
is the only place the rule lives. Do not re-merge the two labels into one
`any()` test, and do not put `STARRED` under the flag. Four tests cover this,
and the fixture has a minority-flagged and a majority-flagged sender — before
those, the `IMPORTANT` branch had no fixture coverage at all, which is exactly
the failure mode this file warns about two sections down.

**A safeguard that did not fire is still shown.** `row_notes()` prints
`important:N/M` on every row that has any, guarded or not, in the table and in
the review file. Weakening a safeguard silently is how a surprise arrives at
trash time; the number is also what tells a user whether another
`--important-guard` mode would move that row.

**The UI is loopback-only and token-gated.** `http.server` binds `0.0.0.0` by
default, which would put a scan trigger — and, if phase 4 is ever built, mail
deletion — on every interface of the machine. `_ui_bind_address()` *raises* on anything else
and there is no `--host` flag to reach it with. Every request carries a
per-launch token, the page included; `Host` and `Origin` are allowlisted, which
is what defeats DNS rebinding; no CORS header is ever emitted. Four tests cover
this. Do not add a convenience flag to any of it.

**The UI page never writes markup.** Every dynamic value goes in through one
`textContent` helper. `test_ui_page_never_writes_markup` fails on the literal
strings `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`,
`eval(` and `new Function` appearing anywhere in `UI_HTML` — including in a
comment, which is how it will first bite you. Phase 3 renders sender-chosen
text on a page that holds a deletion-capable token; the discipline exists now
so it is not being invented then. The page also loads nothing from any external
origin, which is what lets its `Content-Security-Policy` be `default-src
'none'`.

**The UI does not fork the scan.** `/api/scan` calls `cmd_fetch()` itself. That
is what keeps the cache, resumability, drop accounting, circuit breaker and
reconciliation identical between the two front ends. Do not write a second
scan loop for the UI.

**One ranking, three renderings.** The table, `--json` and the review file all
come from `rank_rows()`. The test helper calls it too, so a safeguard test
cannot pass against a private copy while the real ranking regresses. Do not
reintroduce scoring logic anywhere else.

**Scan liveness comes from mtime, never from the pid.** `os.kill(pid, 0)` is
the usual idiom and a trap: on Windows `os.kill` ignores the signal for
anything but `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` and calls `TerminateProcess`, so
the line that asks whether the scan is alive would kill it. The pid in the
status file is for a human to act on. `test_status_never_probes_the_pid` walks
the parsed tree for any `kill` call, so a comment may name it but code may not.

**The status file is telemetry and must never fail a scan.** Every write is
wrapped and swallowed, and goes through a temp file plus `os.replace` so a
reader polling it never sees half a document.

## Layout

```
gmail_audit.py            the entire tool
docs/SETUP.md             OAuth setup, troubleshooting, platform notes
docs/DESIGN-UI.md         proposed web UI (not implemented; Phase 1 shipped)
docs/PLAN-RATE-LIMITER.md how the shared rate limiter works, and why
docs/images/*.svg         hand-authored setup diagrams
tests/test_audit.py       99 offline tests, no API access needed
tests/fixtures/           synthetic headers, example.com domains only
tests/check_diagrams.py   geometric checks on the SVGs
```

### Where things are in `gmail_audit.py`

| Concern | Symbol |
|---|---|
| Subprocess wrapper, retry budgets | `gws()` |
| The only place a subprocess is spawned | `_run()` |
| Shared adaptive pacing | `RateLimiter`, `LIMITER` |
| Throttle vs. transient classification | `THROTTLE`, `TRANSIENT`, `_retryable()` |
| Live counters, drop file, circuit breaker | `FetchProgress`, `PROGRESS` |
| Progress line and reporter thread | `_progress_line()`, `_progress_reporter()` |
| Which headers get requested | `HEADERS`, `ENGAGED_HEADERS` |
| Message ID enumeration | `list_ids()` |
| Per-message header fetch | `get_headers()`, `_safe()` |
| Scan loop and concurrency | `cmd_fetch()`, `_scan()` |
| Scoring | `score_sender()`, `BULK_MAILERS`, `NOREPLY`, `PROTECTED` |
| Ranking and safeguards | `rank_rows()`, `sender_guard()`, `cmd_rank()` |
| The IMPORTANT-label guard and its modes | `important_guards()`, `IMPORTANT_GUARD_MODES` |
| Guard + coverage + signals, one rendering | `row_notes()` |
| The review file, both directions | `write_review()`, `parse_review()`, `load_review_approved()` |
| Cross-process scan status | `StatusWriter`, `read_status()`, `cmd_status()` |
| Mutation | `_trash_one()`, `cmd_trash()`, `cmd_untrash()` |
| Auth preflight and its error classification | `preflight()`, `classify_gws_error()`, `UI_ERRORS`, `UI_HINTS`, `PREFLIGHT_LABELS` |
| First-run readiness check | `cmd_doctor()` |
| The required replied-to safeguard | `require_engaged()`, `load_engaged()` |
| Engaged scan resume checkpoint | `load_engaged_cache()`, `ENGAGED_CACHE` |
| UI server, bind guard, request guards, routing | `make_ui_server()`, `_ui_bind_address()`, `_UIHandler` |
| Scan lifecycle behind the UI | `ScanState`, `_ui_run_scan()`, `cmd_ui()` |
| The served page (inlined CSS and JS) | `UI_HTML` |

## Verifying changes

**Both suites run offline against fixtures. No Gmail access, no quota.** Use
them as the development loop rather than hitting a real mailbox.

```bash
python tests/test_audit.py
python tests/check_diagrams.py
```

CI runs both on every push and PR (`.github/workflows/tests.yml`): Python 3.9,
3.11 and 3.13 on Linux, 3.8 in a container because the runner images no longer
carry it, and one Windows job. The Windows job is not ceremony - the `.cmd`
shim, forced UTF-8 decoding, `os.replace` and the `os.kill` trap are all
Windows-specific, and it is the only place any of them is exercised.

`tests/fixtures/headers.jsonl` is synthetic and uses `example.com` domains
only. It deliberately includes senders that score in Trash range but must be
demoted — a bank, a replied-to vendor, a starred sender — so safeguard
regressions fail loudly.

When adding a signal or safeguard, add a fixture sender that exercises it. A
test that only passes because the fixture lacks the case is worse than none:
the `To`/`Cc` bug below shipped precisely because the fixture bypassed the API.

The `ui` subcommand has the same problem the diagrams do. The `test_ui_*` tests
speak real HTTP to a real loopback socket, so the guards are genuinely covered,
but nothing there can tell you a panel wraps badly or a number is unreadable.
Stub `_run` with a fake mailbox, serve it, and *look at the page* before
claiming a UI change is right. Two layout bugs in phase 2 were invisible to a
green suite and obvious in one screenshot.

## Privacy

`headers.jsonl`, `engaged.txt`, `approved.txt` and the manifests contain real
sender addresses and message IDs from the user's mailbox. They are gitignored.
**Never commit them, never paste their contents into a transcript, and check
`git status` before committing.** Treat them like a mailbox export.

`client_secret.json` and `credentials.enc` are likewise gitignored.

## Environment notes

Each of these caused a real failure during development.

**PowerShell strips quotes from JSON arguments.** PS 5.1 mangles
`'{"userId":"me"}'` into `{userId:me}` when passing to a native `.exe`, giving
*"key must be a string at line 1 column 2"*. Escape them:
`'{\"userId\":\"me\"}'`. Bash passes them through intact, so the same command
works there — which makes this look like an auth bug when it is not.

**`gws` on Windows is a `.cmd` shim.** Python's `subprocess` cannot exec it by
bare name (`WinError 2`). `_find_gws()` resolves the real `.exe`; `GWS_BIN`
overrides it.

**Force UTF-8 on subprocess output.** Header values routinely contain
non-ASCII and Windows' cp1252 default raises `UnicodeDecodeError` mid-fetch.

**Gmail quota is per MINUTE, not per second.** `messages.get` costs 5 units.

*The backoff pathology is fixed; the replacement has its own.* Sustained scans
used to collapse to ~5 msg/s because per-request exponential backoff idled each
worker independently: twelve workers each discovered the ceiling alone and each
slept up to 60s without telling the others. `RateLimiter` replaced that with one
shared pacer. Do not reintroduce a per-worker `delay` local in `gws()`.

**But the ADAPTIVE half is broken, measured against a real mailbox on
2026-09-08.** `THROTTLE_COALESCE` (2s) is shorter than `THROTTLE_HOLD` (15s), so
whenever throttles arrive more often than every 15 seconds the increase branch
is unreachable and the rate collapses to `RATE_MIN`. It did, on every adaptive
run. Pinning with `--rate 8` was 32% faster and stable.

The cause is that the real constraint is **`Units per minute per user`** — read
off a real 429 — so cutting the rate cannot refund units already spent in the
current minute. A 72% rate cut *raised* throttle frequency by 7%. AIMD on
instantaneous rate is the wrong controller for a per-minute budget.
`docs/PLAN-RATE-LIMITER.md`, "Measured against a real mailbox", has the numbers
and the mechanism.

**So a scan pins `RATE_DEFAULT` (8.0 req/s) and the adaptive controller is
opt-in behind `--adaptive`.** That is the inversion of what shipped, and it
stands until the controller is fixed: the broken half must not be what a first
run gets. `--rate 0` still means "adapt", which is what it has always meant.
Three tests cover the default, both routes back to adaptive, and the refusal to
accept `--rate` and `--adaptive` together. That refusal is deliberately *not* an
argparse `mutually_exclusive_group`: its message says what is rejected and not
what to type instead, and since `--rate` now carries a default, "pin at 8 and
also adapt" is a reasonable thing to have believed you were asking for.
`_make_limiter` refuses it with the three real choices spelled out. `--rate`
therefore defaults to `None`, which is the only way to tell a typed `--rate 8`
from the default, and the conflict is about what was typed. Do not trust
`test_fleet_clears_the_twenty_messages_per_second_bar`:
`_simulate()` models a trailing one-second window, under which AIMD converges
by construction.

*The concurrency limit is not fixed and is a different problem.* Concurrency
above ~16 still causes the API to drop messages outright, which silently
undercounts senders and corrupts the ranking — a correctness problem, not a
performance one. The limiter governs rate, not parallelism, and does not repeal
this. The `fetch` default is 12; 16 remains the hard maximum.

Two rules that follow, and that a "simplify" pass will be tempted to break:

- **A throttle and a 5xx are not the same failure.** `THROTTLE` means the fleet
  is too fast: shrink the shared rate, no local sleep. `TRANSIENT` means one
  request failed: sleep locally, leave the rate alone. Re-merging them into one
  `RETRYABLE` regex silently restores the old pathology.
- **One limiter per process, not per pool.** The quota is per-process-per-user,
  so `LIMITER` is a module handle like `GWS`. `cmd_fetch` rebuilds its
  `ThreadPoolExecutor` per batch; per-pool state would re-ramp from the start
  rate on every batch.

See `docs/PLAN-RATE-LIMITER.md` for the design and the deferred items.

**`file://` URLs are blocked in the browser pane; localhost is not.** To view
the SVG diagrams, serve them and navigate to `http://localhost:8765/<file>`:

```bash
python -m http.server 8765 --directory docs/images
```

Do this before claiming a diagram is correct. Structural checks confirm the
file is well-formed; they cannot tell you an arrow points at the wrong box —
which has already happened once here.

## Documentation conventions

`docs/SETUP.md` is written for a user setting the tool up, not for a
contributor. Keep it free of internal history and of anything that identifies a
particular account — no real project IDs, addresses, or mailbox sizes.

The diagrams in `docs/images/` are hand-authored SVG rather than screenshots,
deliberately: no account identifiers to redact, and they degrade gracefully
when Google reorganizes its console. If you edit one, run
`tests/check_diagrams.py`, then *look at it in a browser*.
