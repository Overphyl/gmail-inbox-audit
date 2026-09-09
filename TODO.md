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

**2. Measure the rate ceiling above 8 req/s.** `RATE_DEFAULT` is 8.0 because it
is the only rate ever *observed* stable, not because it was tuned. One
`fetch --rate 12 --limit 2000` against an existing cache says whether pinning
higher stays clean or starts drawing throttles faster than it clears requests.
That number bounds what any correct limiter could achieve, so it is worth more
than more work on the controller. Record it in `docs/PLAN-RATE-LIMITER.md`.

**3. Find out why `fetch` throttles at all.** The open puzzle, and item 4
depends on it. A pinned fetch run reached 5.17 msg/s and drew 646 throttles;
the 1,108-message restore made calls at the same 5.17/s and drew **zero**. Both
estimate to ~1,550 units/minute. So steady-state call rate does not explain the
throttling. The untested suspects are `list_ids` pagination running alongside
the fetch, and concurrency 12 rather than 8. Cheapest experiment: a fetch at
concurrency 8 over an already-cached range, and a fetch with listing separated
from fetching.

**4. Fix the limiter — only after 3.** The mechanism is documented in
`docs/PLAN-RATE-LIMITER.md` ("Measured against a real mailbox"). Do not tune
constants against a mechanism nobody has diagnosed; that is how the current
constants were arrived at. Items 1-3 of "What to change" there are still open;
item 4 is done.

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
- **"Everything up-to-date" and "N messages moved" can both be lies.** Check
  the number against something the operation could not have faked.
