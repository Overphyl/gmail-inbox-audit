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
python tests/test_audit.py     # 140 offline tests, no Gmail access, no quota
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
- **`messages.get` costing more than `untrash`/`modify`**: *first called
  disproved, and that was wrong - see the correction below.* It is 20 units
  against 5, and it is the whole answer.
- **`list_ids` pagination**: disproved. Zero `list` throttles in seven runs,
  and listing finishes before the first `get` anyway.
- **Concurrency**: contributes 8% throughput for 79% more throttles, then
  saturates by 8 workers. Not the cause, and it does not move the ceiling.

The comparison was also not like-for-like: 5.17 msg/s was the fetch's
*achieved* rate after being throttled back from an *offered* 8, while the
restore was latency-bound and only ever *offered* 5.17. One is an outcome, the
other an input.

**The ceiling is Google's published quota, exactly.** Checked against the
Gmail API usage-limits table the same day: the per-user budget is **6,000
quota units per minute** (the quota for Cloud projects created on or after
1 May 2026; older projects kept 15,000) and **`messages.get` costs 20 units,
not 5**. `6000 / 20 =` **300 messages/minute = 5.00 msg/s**. Excluding each
run's opening clean phase, the seven measured steady states average 6,002
units/minute. Not approximately the quota - the quota.

That also reverses one of the four verdicts above. **H2 was right**:
`messages.get` (20) really does cost more than `untrash` (5) and `modify` (5),
and equal call rates are not equal quota rates. The pinned fetch was at 103%
of budget and the restore at 26%, at the identical 5.17 calls/s. The earlier
"disproof" compared the fetch's ceiling against a restore that was
latency-bound at a quarter of its own, which tests nothing.

Costs worth knowing before pacing anything: `list` 5, `get` 20, `trash` **20**,
`untrash` 5, `modify` 5. A restore is four times cheaper per message than a
scan; a trash run is exactly as expensive as one; a 70-page enumeration is 6%
of one minute. The keyring-surcharge hypothesis is dead, and with it the
suggestion that `DESIGN-UI.md` rejected direct HTTPS on a false premise.

**4. ~~Fix the limiter.~~ Done, 2026-09-09.** The limiter paces **quota units
per minute** and charges each call its published price, so one constant
(`UNITS_PER_MINUTE = 6000`) paces every command correctly: 300 messages/minute
for a scan or a trash, 1,200 calls/minute for an untrash or a modify. It is not
adaptive, because a published constant has nothing to search for. `--rate`
still pins req/s as the escape hatch and `--adaptive` still reaches the broken
AIMD controller; `--budget` refuses to combine with either.

Measured on the reference mailbox with the shipped default, no flags:

| command | messages | throughput | of budget | throttles | lost |
|---|---|---|---|---|---|
| `fetch` | 900 | 5.02 msg/s | 100% | 0 | 0 |
| `fetch` | 557 | 5.00 msg/s | 100% | 0 | 0 |
| `trash` | 557 | 5.00 msg/s | 100% | 1 | 0 |
| `untrash` | 557 | 1.47 msg/s | 15% | 0 | 0 |

The same fetch work under the old `--rate 8` default drew 488 throttles and
lost 2 messages per 2,000. The restore is latency-bound at 15% of budget, which
is the case a single req/s number could never pace: `--rate 8` was 160% of
budget on a scan and 40% of it on a restore at the same time.

The offline fleet simulation was replaced too. It metered arrivals over a
trailing *second*, under which AIMD converges by construction — which is why
green tests never predicted a real run. It now meters units over a minute, and
reproduces the pathology: pinned at 8 req/s the fleet delivers the same 300
msg/min while wasting 37.6% of its requests.

**Still open, and small:** `server_errors` is not in the status payload, so
`TRANSIENT` retries are invisible on disk. The 557-message restore ran at 1.47
msg/s where an earlier one managed 2.58 with *half* the workers, and the
likeliest cause is those retries taking local backoff — but nothing recorded
can confirm it. See the end of `docs/PLAN-RATE-LIMITER.md`.

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
  matched by seven runs to within 5%. A full 35,000-message inbox is therefore
  a ~2 hour scan at best, and the shipped pacing spends part of that drawing
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
- **Compare like with like.** The whole puzzle was one table putting an
  *achieved* rate next to an *offered* rate in the same column.
- **"Everything up-to-date" and "N messages moved" can both be lies.** Check
  the number against something the operation could not have faked.
