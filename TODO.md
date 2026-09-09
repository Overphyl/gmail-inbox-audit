# State of the project

**What this file is for:** handing the project to someone who was not here.
It records what has been *verified*, what is next, and what is deliberately
not being built. `CLAUDE.md` holds the rules; this holds the state.

**If you change the state, update this file in the same commit.** A stale
status file is worse than none: it is confidently wrong, and the next person
acts on it. Same reason the review file says `FILTERED` when it is truncated.

Last updated: 2026-09-09.

---

## Where this stands

The rate-limiter work is **finished**. The limiter paces Gmail's actual
constraint - quota units per minute, priced per method - and has been measured
end to end on a real mailbox across `fetch`, `trash` and `untrash`. Items 2, 3
and 4 below are closed; `docs/PLAN-RATE-LIMITER.md` has the runs, the numbers
and the reasoning, and `CLAUDE.md` has the invariants that came out of it.

**The one open task is not a code task.** It is item 1: working through the
trash candidates, which is the repo owner's judgement to exercise, not a
contributor's.

If you are picking this up cold: read `CLAUDE.md`'s **Hard rules** first, run
both suites, and do not start on the "Deferred, deliberately" list without
asking.

---

## Start here

```bash
python tests/test_audit.py     # 145 offline tests, no Gmail access, no quota
python tests/check_diagrams.py
```

Both must pass before and after any change. Read the **Hard rules** section of
`CLAUDE.md` first: they are safety properties, several were bought with real
bugs, and "simplify" requests do not include them.

---

## Verified against a real mailbox

Measured 2026-09-08 and 2026-09-09 on an inbox of 34,953 messages / 5,192
senders (55,689 messages in the account). These paths have run for real, not
only against fixtures.

| | evidence |
|---|---|
| `doctor` | reported ready, named the next command |
| `fetch` | 34,954 messages cached |
| `engaged` | 4,764 sent messages, 1 drop, resumable |
| `rank`, safeguards, review file | 5,192 senders ranked |
| `--important-guard` all three modes | off / majority / any compared |
| `status` | read a scan from another terminal |
| `trash --execute` | 1,108 messages, 2 senders, confirmed in Gmail |
| manifest → `untrash` → `INBOX` restored | 1,107/1,108, confirmed in the inbox |
| throttle diagnosis, rate and concurrency sweep | 7 runs x 2,000 messages, one at a time |
| unit-aware limiter, all three costs | fetch 900+557, trash 557, untrash 557 - 1 throttle total |

The one failure was a `Precondition check failed` race, retried by hand and now
classified `TRANSIENT`.

## Shipped but NOT exercised for real

Green in tests; nobody has run them against a mailbox. Treat a first real run
as a source of bugs, because every previous first run was.

- `ui` — the localhost server, preflight and scan progress
- `rank --review --min-score N`
- `baseline`

`untrash --cache` and `trash` on a label-recording manifest came off this list
on 2026-09-09: the 557-message round trip on the nominated test sender
exercised both, and all 557 were confirmed back in the inbox by query.

---

## Next

**1. Work through the trash candidates.** Not a code task, and the only open
one. `rank --review --min-score 6` writes a 331-row file: 146 recommended for
Trash, 185 that a safeguard held back. Every batch carries labels, so the undo
is faithful. This is still the first real exercise of `--min-score`.

### Open questions, none urgent

These are worth a decision, not a sprint. Each is one measurement or one
conversation, and none blocks anything.

- **Does `fetch` still want concurrency 12?** A scan is quota-bound at 5
  calls/second and per-call latency is ~1.3s, so roughly 7 workers saturate the
  budget. Twelve was chosen when the constraint was believed to be req/s, and
  more workers now demonstrably *raise* per-call latency on the mutation path.
  Nobody has measured whether the same curve bites a fetch. Cheap: two runs.
- **Do `--rate` and `--adaptive` still earn their place?** Neither is the
  default any more and `--adaptive` is documented as broken. They are three
  flags, a mutual-exclusion check and a mode in the limiter. Keeping them is
  defensible - `--rate` is the escape hatch for a mailbox this tool has not
  seen - but that is the owner's call, not a cleanup a contributor should make
  unasked.
- **What drives per-call latency as concurrency rises?** 1.29s at 8 workers,
  4.94s at 16, with Gmail uninvolved. Probably `gws` process startup and its
  per-invocation keyring load. Answering it needs process-level instrumentation
  this tool has no business carrying, and it changes no default.
- **Three paths have still never run for real** - see the list above.

---

## Recently completed

Summarised, because the detail belongs in the design document rather than in a
status file. `docs/PLAN-RATE-LIMITER.md` has all of it.

**2. Measure the rate ceiling above 8 req/s.** Done 2026-09-09: there is
nothing up there. Seven runs of `fetch --limit 2000`, one at a time, varying
one parameter each. Every configuration between 5 and 16 req/s and between 4
and 12 workers landed within 14% of the same throughput, because the ceiling
is not a rate.

**3. Find out why `fetch` throttles at all.** Done 2026-09-09. `gws()` now
keeps the first throttle texts per run and counts them per API method, and the
API's own sentence named the constraint: `Units per minute per user`.

The mechanism is a **per-minute unit budget spent by successful calls**. Two
constants this repo had carried since its first commit were wrong - the budget
is 6,000 units/minute, not 15,000, and `messages.get` costs **20** units, not
5. `6000 / 20 = 300` messages a minute, which is exactly what every run
measured. Three of the four standing hypotheses were disproved; the fourth
(that `get` costs more than `untrash`/`modify`) was right, was wrongly
dismissed first, and is the whole answer.

**4. Fix the limiter.** Done 2026-09-09. It paces quota units per minute and
charges each call its published price, so one constant paces every command
correctly: 300 messages/minute for a scan or a trash, 1,200 calls/minute for an
untrash or a modify. Not adaptive - a published constant has nothing to search
for. Measured on the reference mailbox with no flags:

| command | messages | throughput | of budget | throttles | lost |
|---|---|---|---|---|---|
| `fetch` | 900 | 5.02 msg/s | 100% | 0 | 0 |
| `fetch` | 557 | 5.00 msg/s | 100% | 0 | 0 |
| `trash` | 557 | 5.00 msg/s | 100% | 1 | 0 |
| `untrash` | 557 | 3.09 msg/s | 15% | 0 | 0 |

The same fetch work under the old `--rate 8` default drew 488 throttles and
lost 2 messages per 2,000.

**5. Make the transient retries visible.** Done 2026-09-09, after the restore
above raised a question no counter could answer. `server_errors`, a per-method
split, sampled texts and a `backoff_seconds` accumulator reach the status file,
and all four long-running commands report pacing - `trash` and `untrash`
previously reported none at all. Its first use refuted the hypothesis it was
built to confirm: backoff explained 4% of the anomaly it was meant to explain,
and per-call latency rising with concurrency explained the rest.

---

## Deferred, deliberately

**Do not build these without asking the repo owner.** They are decisions, not
backlog.

- **Phase 3b** (review table in the browser) and **Phase 4** (execute and undo
  in the browser). Deferred together and on purpose: building 3b without 4
  recreates the seam the review file removed. See `docs/DESIGN-UI.md`.
- **Phase 5** (incremental history scans). *Downgraded, not pending.* Gmail
  expires a `historyId` in about a week and this tool's cadence is annual, so
  every real run would take the documented full-scan fallback. The resumable
  caches already deliver most of it.
- **A single-pass `modify`** that clears `TRASH` and adds `INBOX` together.
  Possibly works, untested, and would put `removeLabelIds` in the codebase -
  turning a greppable invariant into a judgement call on every future edit, to
  save about two minutes on an annual run.

---

## Known limitations

- `untrash` restores only `INBOX` (`RESTORE_LABELS`). Other labels survived the
  measured round trip untouched. If another turns out to be lost, the manifest
  already records every label and that tuple is the one edit.
- The adaptive limiter is broken and opt-in behind `--adaptive`. Nothing
  uses it: the default paces the quota budget, which needs no search.
- **The mailbox is quota-bound at 300 messages/minute (5.0 msg/s) and no
  client-side knob raises it.** That is `6,000 quota units per minute per user`
  divided by the 20 units a `messages.get` costs - Google's published numbers,
  matched by seven runs to within 5%. A full 35,000-message inbox is a ~2 hour
  scan, and that is the API's floor rather than the tool's: the shipped pacing
  now spends the whole budget and draws essentially nothing.
- A restore is four times cheaper per message than a scan (`untrash` and
  `modify` cost 5 units each against a `get`'s 20), and a `trash` costs exactly
  as much as a scan. Any reasoning about how long something will take starts
  there, not with requests per second.
- `engaged.txt` on the reference mailbox is 4,763 of 4,764 sent messages: the
  replied-to safeguard is 99.98% complete, not complete.
- Concurrency above ~16 makes the API drop messages. The `fetch` default is 12
  and 16 is the hard maximum. This is a correctness problem, not a speed one.
- Separately and for a different reason, concurrency above ~8 makes the
  *mutation* path slower: per-call latency rose from 1.29s to 4.94s between 8
  and 16 workers, with Gmail uninvolved. `trash`, `untrash` and `engaged`
  default to 8 and should stay there. Two ceilings, two mechanisms; see
  `CLAUDE.md`.

---

## Lessons that cost something

Recorded because each was paid for once and should not be paid for twice.

- **Every defect found by running for real was in reporting or record-keeping**,
  never in the code that moves mail. Seven of them. `messages.trash` was correct
  from the first commit.
- **A test that only passes because the fixture lacks the case is worse than
  none.** The `IMPORTANT` guard had no fixture coverage at all and immunised
  100% of high-volume senders.
- **The Windows and Python 3.8 CI jobs are not ceremony.** Each has caught a
  defect no other job could see, and neither was a version incompatibility in
  the tool - both were the harness assuming its own environment.
- **An error matching neither retry pattern gets zero retries, silently.**
  Three such classes turned up in one day.
- **A counter cannot tell you why.** Four hypotheses about the throttling stood
  for a day; the error text settled the mechanism in one run, and `gws()` had
  been holding that text and discarding it the whole time. When a measurement
  is confusing, check whether the code is already touching the answer.
- **Check the published constant before modelling around it.** The whole
  puzzle came from two wrong numbers - a 15,000 unit/minute budget that is
  6,000, and a 5-unit `messages.get` that costs 20 - carried in this repo's own
  documentation for weeks and never looked up. Days of measurement produced a
  ceiling that one page of Google's docs states outright, and a *plausible*
  cost of 45-50 units per call had already been derived to explain the gap.
- **A hypothesis tested against a free variable is not tested.** H2 was
  "disproved" by comparing a workload pinned against its ceiling with one that
  was latency-bound at a quarter of its own. It was right all along.
- **More workers can mean less work.** Doubling mutation concurrency from 8 to
  16 nearly halved throughput, because per-call latency rose from 1.29s to
  4.94s. One `gws` process per API call means the fleet competes with itself
  for the machine long before it competes for quota.
- **Compare like with like.** The whole puzzle was one table putting an
  *achieved* rate next to an *offered* rate in the same column.
- **"Everything up-to-date" and "N messages moved" can both be lies.** Check
  the number against something the operation could not have faked.
