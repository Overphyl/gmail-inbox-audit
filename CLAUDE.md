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

## Where the project is

`TODO.md` records what has been verified against a real mailbox, what is next,
and what is deferred on purpose. Read it before deciding what to work on, and
update it in the same commit as any change to that state. It is the answer to
"what should I do next"; this file is the answer to "what must I not break".

## Hard rules

These are safety properties, not preferences. Do not relax any of them for
convenience, and do not assume a request to "clean up" or "simplify" includes
them.

**No permanent deletion, ever.** The only mutating API calls are
`messages.trash`, `messages.untrash` and `messages.modify`.
`messages.delete` and `messages.batchDelete` must never appear in this codebase. They require the
`https://mail.google.com/` scope; the tool authenticates with `gmail.modify`,
under which Google itself refuses permanent deletion. `test_no_permanent_delete_code_path`
greps the source and fails if either appears. If that test is in your way, you
are doing something wrong.

`messages.modify` was added to that list deliberately, on 2026-09-09, and is
**add-only**: it may pass `addLabelIds` and must never pass `removeLabelIds`.
It exists for one job, restoring the `INBOX` label that `messages.untrash`
does not put back (measured: a real 1,108-message restore landed in All Mail,
with `TRASH` gone and `INBOX` absent). An undo that cannot put things back is
not an undo. The rule it widens is about *permanent deletion*, and adding a
label cannot delete anything, which is why the purpose survives the amendment.
`test_modify_is_add_only` walks the parsed tree and fails on any
`removeLabelIds` in the source. Removing a label is still a thing this tool
does not do.

Only `INBOX` is restored (`RESTORE_LABELS`). On the measured round trip
`CATEGORY_*` and `IMPORTANT` survived trash and untrash untouched, so re-adding
them is a no-op; re-adding `UNREAD` would be worse than a no-op, resurrecting a
read state from whenever the cache was built rather than from just before the
trash. The manifest records every label anyway, so if another turns out to be
lost the evidence is on disk and that tuple is the one edit.

Untrash and relabel are **one unit of work**, not two passes: a message that
left Trash but never got its `INBOX` back is half restored, and counting it as
a success would be the same lie as counting attempts.

`untrash --cache` recovers labels for manifest rows that predate label
recording. The cache those messages were selected from recorded their labels at
fetch time, so this reads what was written down rather than assuming anything,
and it is consulted **only** for rows the manifest cannot answer - a manifest
this version wrote never touches it. That restriction is not an optimisation: a
cache rebuilt since the trash describes the mailbox now, not the mailbox then,
which is exactly why the manifest records labels itself. Three tests, one
asserting the cache is not read when the manifest is complete.

**`--params` and `--json` are different channels.** `--params` carries path and
query parameters; the request body goes in `--json`. Passing `addLabelIds`
through `--params` does not fail loudly - `gws` warns that the parameter is not
marked as repeated and stringifies it, so the API receives a single label
literally named `["INBOX"]` and answers `Invalid label`. `metadataHeaders` in
`get_headers` is an array through `--params` and works, because it is a query
parameter; that similarity is the trap.

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

**A parse error names the mistake, not just the symptom.** The commonest way
to get the review file wrong is to leave the `.` where it is and add a `t`
beside it, in the flag column. `parse_review` checks whether the token it
rejected is itself a valid mark and says the mark is column 1 and nothing else;
`'t' is not an address` is true and useless. Two tests: the misplaced mark, and
a genuinely mangled address, so the clearer message cannot swallow the case it
was carved out of.

**A marked sender that matches nothing is an error, not a skip.** The review
file is generated from the cache, so a `t` on a sender with no cached messages
was typed by hand. A transposed domain is still a syntactically valid address,
so no parser catches it; refusing the run is what stops it being silent. The
plain `--senders` path keeps the softer note, because there the list is
hand-written by design.

**Manifest before mutation, and one manifest per run.** `cmd_trash` writes
every target message ID to disk *before* trashing anything, so an interrupted
run still leaves a complete undo list for `cmd_untrash`.

It used to write one fixed path with `"w"`, so trashing a second batch
destroyed the first batch's undo list and the only warning was a person
remembering to copy the file. The undo list is the recovery path for an
operation that moves real mail; it must not be the thing that quietly goes
missing. `manifest_path()` names it for the run's timestamp and never returns a
path that exists; an explicit `--manifest` is honoured exactly, because a named
path is a decision. `cmd_untrash` with no `--manifest` takes
`latest_manifest()` - newest by mtime, so a manifest renamed to something
meaningful is still undoable - and *prints which one it chose*, since restoring
the wrong run is the failure that command exists to prevent and a dry run is
the default. Five tests.

**A mutation count is a count of successes, never of attempts.** `_safe_mutate`
swallows the error so one bad message cannot abandon the batch, which means the
loop body runs whether the call worked or not. Both mutating commands used to
count iterations, so a run in which every single call failed still printed
"N messages moved to Trash" and exited 0 - the one number a person uses to
decide whether the mutation worked, wrong only ever in the unsafe direction.
Count the return value: `_safe_mutate` gives the id on success and `None` on
failure. `_report_mutations()` is the single closing line for both commands and
`sys.exit`s when anything failed, because a run that did not do what was asked
must not end quietly under a success line. Three tests, one of them asserting a
clean run still exits 0. Do not reintroduce `for _ in ex.map(...)` on a
mutating path.

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

**A throttle is evidence, and it is kept.** `gws()` held the API's own stderr
when it classified a `THROTTLE` and then discarded it, so 646 throttles in a
single pinned run left nothing on disk and every claim in
`docs/PLAN-RATE-LIMITER.md` about *why* the fleet is throttled was inference
from a counter. The only two texts ever read arrived by accident: a message
exhausted all twelve throttle retries and became a drop. `_record_throttle()`
now keeps the first `THROTTLE_SAMPLES` (5) texts per run in the limiter, in
the status file and in the drop file, tagged `"event": "throttle"` so a person
reading that file for IDs to retry can still tell the two apart. Capped
because the status file is rewritten every two seconds: telemetry that grows
with the run is a different bug.

`_record_throttle()` calls `on_throttle()` **first and unguarded** - that is
control, the pause and the possible decrease, and a real limiter bug must
still surface - and only then records, inside a swallow. A throttle that
cannot be written down still has to pace the fleet.
`test_a_scan_completes_when_the_throttle_capture_raises` removes the guard's
excuse by making the capture path itself raise.

**Throttles are counted per API method, and `list` is not `get`.** They were
one counter, so a throttle drawn by `list_ids` pagination read exactly like
one drawn by a per-message fetch - and those are different answers to the
question the counter exists to settle. `_call_kind()` reads the verb straight
out of the argument list, so nothing is plumbed through the call sites. Note
what the split can and cannot see: `list_ids` passes `--page-all`, so one
`gws` call paginates the whole mailbox and a 429 that `gws` retries *inside*
that call is invisible here. A `list` throttle in the counter is one that
failed the whole call. Also, `list_ids` runs before `cmd_fetch` builds its
`FetchProgress`, so listing throttles reach the limiter and the status file
but not the drop file.

**Captured throttle text is redacted at the capture, not at the read.**
`redact()` strips addresses and Gmail's lowercase-hex message IDs. This text
exists to be *pasted* - into a bug report, into
`docs/PLAN-RATE-LIMITER.md` - and a throttle stderr can echo the request that
drew it, which carries an ID and sometimes a query naming a sender. Deciding
once at the write is the same rule the drop file already follows by recording
IDs and never headers.

**A mutation reports progress like a scan does.** `cmd_trash` and `cmd_untrash`
build a `FetchProgress`, run through `_mutate()` and publish through
`StatusWriter`, so the live line, the rate, the ETA and `gmail_audit.py status`
work identically on a restore and on a fetch. Every long-running command here
published progress *except* the two that move mail, which is backwards: a scan
you cannot see is an annoyance, a mutation you cannot see is the one you most
want to watch. A real 1,108-message restore printed one line and then nothing
for four minutes. `trash` starts its reporter per batch rather than around the
loop, because the live line writes to stderr with a carriage return and would
scribble over the `[y/N]` prompt. The test asserts a *mid-run* tick, not just
the closing write: a run that went silent and then published a finished file
would pass the weaker check, and that is exactly the bug.

No drop file on these paths: the manifest already names every target, and both
commands are idempotent, so re-running retries exactly the failures. The
circuit breaker does apply, and counts only *non-retryable* failures - a
throttle means the fleet is too fast, not that the run is doomed, and counting
it would abort long runs on a healthy mailbox. Two tests, one per direction.

## Layout

```
gmail_audit.py            the entire tool
TODO.md                   project state: verified, next, deferred
docs/SETUP.md             OAuth setup, troubleshooting, platform notes
docs/DESIGN-UI.md         proposed web UI (not implemented; Phase 1 shipped)
docs/PLAN-RATE-LIMITER.md how the shared rate limiter works, and why
docs/images/*.svg         hand-authored setup diagrams
tests/test_audit.py       131 offline tests, no API access needed
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
| Throttle capture, and its redaction | `_record_throttle()`, `_call_kind()`, `redact()`, `_report_throttles()` |
| Mutation | `_trash_one()`, `_untrash_one()`, `_relabel_one()`, `_mutate()` |
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

**Force UTF-8 on subprocess output, and on every file.** Header values
routinely contain non-ASCII and Windows' cp1252 default raises
`UnicodeDecodeError` mid-fetch. The same applies to the files this tool writes
and reads back: the manifest is written with `ensure_ascii=False`, so a single
`open()` without `encoding="utf-8"` is a crash on somebody's mailbox and
nobody else's. `test_every_text_file_names_its_encoding` walks the parsed tree
and fails on any text-mode `open()` that omits it.

**Windows resets a connection closed with an unread request body.** Closing a
socket that still has buffered received bytes sends RST rather than FIN, so the
client gets `WinError 10053` instead of the response the server actually wrote.
Every rejection path in `_UIHandler` replies without reading the body, which is
exactly when this bites: a POST to `/api/scan` with a bad token or a foreign
Origin carries JSON nobody reads, the 403 is written correctly and then
destroyed by the close, and the page shows a network error in place of the
reason. `_send()` therefore calls `_drain_request_body()` *before* replying, and
`_read_json()` claims the body first so it is never read twice - a double read
blocks forever on bytes that are not coming, which is what four scan tests do
if you remove that line. Linux cannot show either failure: the bytes are
already delivered before the close, so the client gets its 403 either way. The
tests drive the handler against a fake socket and ask the portable question
(was the body consumed) rather than the platform-specific one.

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
- **An error matching neither pattern gets zero retries**, which is correct for
  a revoked token and wrong for a race. `Precondition check failed` was the
  third such class found by running for real, after the keyring noise in the
  drop file: a restore untrashes a message and then adds `INBOX` back, and
  Gmail occasionally has not committed the untrash when the modify arrives.
  Measured once in 1,108, and the retry succeeded by hand on a message with no
  `SPAM`, `DRAFT` or `TRASH` label to explain a permanent refusal, so it is
  `TRANSIENT` and not `THROTTLE`: one request lost a race, the fleet was not
  too fast. When a new failure shows up in a drop file or a `!` line, check it
  against both patterns before assuming it was retried.
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
