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

**Throughput collapses under sustained load.** Measured on a real inbox:

| Condition | Rate |
|---|---|
| Short burst, concurrency 16 | 35.7 msg/s |
| Short burst, concurrency 12 | ~26 msg/s (projected) |
| **Sustained over 53 minutes, concurrency 12** | **5.1 msg/s** |

The burst benchmark measured a fresh quota bucket. Sustained, the per-minute
limit binds continuously and per-request exponential backoff — which climbs to
60 seconds — idles every worker independently. The tool spends most of its wall
clock asleep rather than near the quota ceiling.

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

### Global rate limiter — the primary fix

Replace per-request exponential backoff with a **single token bucket shared by
all workers**, sized just under the observed quota ceiling. Today twelve
workers each discover the limit independently and each sleep up to a minute;
one coordinated limiter keeps the fleet near the ceiling instead of oscillating
between over it and asleep.

Make it **adaptive**: ramp until 429s appear, back off, settle. Quota varies by
project, so a hardcoded constant would be wrong for someone else's setup. The
current concurrency guidance (12–16) is really a proxy for a rate limit that
should be enforced directly.

### Direct HTTPS instead of subprocess-per-message

Each header fetch currently spawns a `gws` process, costing roughly 200ms
before any network work. Calling the Gmail API directly with `urllib`, using a
token obtained via `gws`, removes that ceiling and puts every request in one
process where the shared limiter actually works.

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
| 1 | Rate limiter | Independent of the UI; fixes today's pain and speeds all later testing |
| 2 | Server, preflight, scan progress | No deletion path exists yet |
| 3 | Review table and selection | Replaces `approved.txt` |
| 4 | Execute and undo | Token and escaping must land *before* this |
| 5 | Incremental history scans | Makes ongoing use cheap |

The rate limiter leads deliberately. It is the problem actually being felt, it
carries no UI risk, and every later phase is easier to test when a scan takes
minutes instead of an hour.

---

## Risks and open questions

**Handling the refresh token.** Direct HTTPS calls require the tool to obtain
an access token from the credentials `gws` stores. That is a meaningful
increase in what the tool touches. Falling back to subprocess-per-message keeps
credentials entirely inside `gws` at a large throughput cost. Worth deciding
explicitly rather than drifting into.

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
