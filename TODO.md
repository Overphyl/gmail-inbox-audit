# State of the project

**What this file is for:** handing the project to someone who was not here.
It records what has been *verified*, what is next, and what is deliberately
not being built. `CLAUDE.md` holds the rules; this holds the state.

**If you change the state, update this file in the same commit.** A stale
status file is worse than none: it is confidently wrong, and the next person
acts on it. Same reason the review file says `FILTERED` when it is truncated.

Last updated: 2026-09-09.

---

## Start here

```bash
python tests/test_audit.py     # 131 offline tests, no Gmail access, no quota
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

The one failure was a `Precondition check failed` race, retried by hand and now
classified `TRANSIENT`.

## Shipped but NOT exercised for real

Green in tests; nobody has run them against a mailbox. Treat a first real run
as a source of bugs, because every previous first run was.

- `ui` — the localhost server, preflight and scan progress
- `rank --review --min-score N`
- `untrash --cache` (the field repair was done with an ad-hoc script instead)
- `baseline`
- `trash` on a batch whose manifest records labels (the verified run predates
  label recording)

---

## Next

**1. Work through the 146 trash candidates.** Not a code task. `rank --review
--min-score 6` writes a 331-row file: 146 recommended for Trash, 185 that a
safeguard held back. Every batch from here carries labels, so the undo is
faithful. This is also the first real exercise of `--min-score` and of a
label-recording manifest.

**2. ~~Measure the rate ceiling above 8 req/s.~~ Done, 2026-09-09: there is
nothing up there.** Seven runs of `fetch --limit 2000` over a throwaway cache,
one at a time, varying one parameter each. Pinning at 12 gives 5.70 msg/s and
at 16 gives 5.61, against 5.16 at 8 — and all three lose messages to quota. The
shipped default (`--rate 8 --concurrency 12`) delivers 5.57 msg/s, within 2.3%
of the fastest configuration measured anywhere. **`RATE_DEFAULT` should not be
raised**, and the table is in `docs/PLAN-RATE-LIMITER.md`.

The interesting direction is down. `--rate 5` delivers 5.00 msg/s — 88% of the
fastest run — for **3 throttles instead of 504, and zero lost messages instead
of two**. Over a full inbox that is about twenty minutes slower and it is the
only configuration measured that fetched 2,000 of 2,000. Proposed, not changed:
it belongs to item 4.

**3. ~~Find out why `fetch` throttles at all.~~ Done, 2026-09-09. All four
suspects were wrong.** `gws()` now keeps the first five throttle texts per run
and counts throttles per API method, and the API's own sentence is
`Quota exceeded ... limit 'Units per minute per user'`.

The constraint is a **per-minute unit budget spent by successful calls**, not a
rate limit. Across a 3.2x range of pinned rate and a 3x range of concurrency,
successes clamp to 300-342 per minute while attempts range 300-479: every extra
request bought a rejection, not a message. Each run runs clean until the budget
drains — 144s at `--rate 5`, 20s at `--rate 16` — and then oscillates on a
60-second period as the window rolls over.

- **A second process sharing quota**: disproved. The same configuration
  reproduced 488 throttles with nothing else touching the account.
- **`messages.get` costing more than `untrash`/`modify`**: disproved. The fetch
  sustains 300-342 calls/min; the restore ran at 310, inside that band.
- **`list_ids` pagination**: disproved. Zero `list` throttles in seven runs,
  and listing finishes before the first `get` anyway.
- **Concurrency**: contributes 8% throughput for 79% more throttles, then
  saturates by 8 workers. Not the cause, and it does not move the ceiling.

The original puzzle was a category error: 5.17 msg/s was the fetch's *achieved*
rate after being throttled back from an *offered* 8, while the restore was
latency-bound and only ever *offered* 5.17. It drew no throttles because it sat
just under a ceiling nobody had measured.

**One number is still unknown and cannot be read from inside this tool.** The
ceiling is ~307 successful `get`/min, which at the documented 5 units/call is
1,535 units/min — 10% of Gmail's documented 15,000/min/user. Either this
project's budget is that small, or one `get` through `gws` costs ~45-50 units
rather than 5. The Cloud console quota page settles it in one look, and the
answer bears on the "subprocess per message" decision in `DESIGN-UI.md`.

**4. Fix the limiter — now unblocked, and the target has changed.** Item 3
delivered the mechanism, so this is no longer gated. Read
`docs/PLAN-RATE-LIMITER.md`, "the throttle, diagnosed", first: AIMD on
instantaneous rate is the wrong controller for a per-minute budget, so items
1-2 of "What to change" prevent the collapse without addressing it. The
budget looks like a constant, which means the minimal correct controller may
not be adaptive at all — `--rate 5` already is one. Do not tune constants
without re-measuring; that is how the current ones were arrived at.

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
- The adaptive limiter is broken and opt-in behind `--adaptive`.
- **The mailbox is quota-bound at about 5.5 messages/second and no client-side
  knob raises it.** Measured seven ways on 2026-09-09; the constraint is a
  per-minute unit budget, not a rate. A full 35,000-message inbox is therefore
  a ~2 hour scan at best, and the default pacing spends part of that drawing
  throttles and losing one to three messages per two thousand.
- `engaged.txt` on the reference mailbox is 4,763 of 4,764 sent messages: the
  replied-to safeguard is 99.98% complete, not complete.
- Concurrency above ~16 makes the API drop messages. The `fetch` default is 12
  and 16 is the hard maximum. This is a correctness problem, not a speed one.

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
  for a day and all four were wrong; the error text settled it in one run, and
  `gws()` had been holding that text and discarding it the whole time. When a
  measurement is confusing, check whether the code is already touching the
  answer.
- **Compare like with like.** The whole puzzle was one table putting an
  *achieved* rate next to an *offered* rate in the same column.
- **"Everything up-to-date" and "N messages moved" can both be lies.** Check
  the number against something the operation could not have faked.
