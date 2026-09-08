# gmail-inbox-audit

Audit a large Gmail inbox by **message headers only**, rank senders by spam
signals, and bulk-trash the ones you approve — with recovery at every step.

Built for mailboxes with tens of thousands of messages, where clicking through
the Gmail UI is not realistic.

```
sender                              n  score  recommendation            signals
news@deals.example.com             60      8  Trash                     List-Unsubscribe, Precedence:bulk, bulk-mailer, volume:60
no-reply@sketchy.example.net       12      9  Trash                     List-Unsubscribe, no-reply, SPF/DKIM fail, volume:12
alerts@mybank.example.com          30      8  Review(protected-domain)  List-Unsubscribe, Precedence:bulk, no-reply, volume:30
newsletter@vendor.example.org      25      6  Review(replied-to)        List-Unsubscribe, Precedence:bulk, volume:25
promo@shop.example.com             15      6  Review(starred/important) List-Unsubscribe, Precedence:bulk, volume:15
jane@friend.example.com             4      0  Keep
```

Note the middle three: they scored in Trash range and were **demoted to
Review** because you bank with them, you reply to them, or you starred them.
That behaviour is the point of this tool.

---

## Design principles

**Headers only.** Every read uses the Gmail API's `format=metadata` with an
explicit `metadataHeaders` allowlist. In that mode the API returns no body
payload at all — message bodies are never fetched, never stored, never seen by
a human or a model. This is enforced by the API, not by convention.

**Trash, never delete.** The tool calls `messages.trash` and nothing else.
There is no `messages.delete` or `batchDelete` code path in the file. Combined
with the `gmail.modify` scope (see below), permanent deletion is *impossible*,
not merely discouraged.

**Absent capability beats remembered intent.** Rather than being careful about
not deleting, the tool cannot delete.

**Approval is a list, not a threshold.** `trash` refuses to run without an
explicit file of approved sender addresses. It will not act on "everything
scoring above 6".

**Manifest before mutation.** Every message ID is written to disk *before*
anything is trashed, so a complete undo list survives an interrupted run.
`untrash` replays it.

---

## Install

- Node.js 18+ — runtime for `gws`
- [`gws`](https://github.com/googleworkspace/google-workspace-cli) — the Google Workspace CLI
- Python 3.8+ — **standard library only**, no pip install, no virtualenv
- Google Cloud SDK (`gcloud`) — needed once, for API enablement
- A Google Cloud project — free, no billing account required

<details>
<summary><b>Windows</b></summary>

```powershell
winget install OpenJS.NodeJS
winget install Python.Python.3.12
winget install Google.CloudSDK
```

Open a **new terminal** so PATH refreshes, then:

```powershell
npm install -g @googleworkspace/cli
```
</details>

<details>
<summary><b>macOS</b></summary>

```bash
brew install node python
brew install --cask google-cloud-sdk
npm install -g @googleworkspace/cli
```
</details>

<details>
<summary><b>Linux</b></summary>

Node.js 18+ and Python 3.8+ from your package manager, the Cloud SDK per
<https://cloud.google.com/sdk/docs/install>, then:

```bash
npm install -g @googleworkspace/cli
```
</details>

Then get the tool:

```bash
git clone https://github.com/Overphyl/gmail-inbox-audit.git
cd gmail-inbox-audit
python tests/test_audit.py      # optional: 81 offline tests, no API access
```

## Setup

OAuth setup is the genuinely fiddly part and has several traps that produce
confusing failures. **[docs/SETUP.md](docs/SETUP.md) walks through all of it**,
including:

- why the OAuth client must be **Desktop app** type (and why Web / Chrome
  extension / Android cannot work)
- the **authorized-domain trap** — and the one-line way to avoid needing a
  domain you own
- why restricted scopes get **silently dropped** at consent
- why `gws auth status` **lies** about which scopes you have

### Scope: use `gmail.modify`

```bash
gws auth login --scopes https://www.googleapis.com/auth/gmail.modify,openid,https://www.googleapis.com/auth/userinfo.email
```

| Method | Required scope |
|---|---|
| `messages.trash` | `gmail.modify` **or** `https://mail.google.com/` |
| `messages.delete` (permanent) | `https://mail.google.com/` **only** |
| `messages.batchDelete` | `https://mail.google.com/` **only** |

**Never authenticate with `https://mail.google.com/`.** With `gmail.modify`,
trash works and permanent deletion is refused by Google itself. Everything
lands in Trash with 30-day recovery.

Verify with a real API call — not `gws auth status`, which reports the scopes
that were *requested* rather than the ones that were *granted*:

```bash
gws gmail users getProfile --params '{"userId":"me"}'
```

---

## Usage

### 0. Check you are ready

```bash
python gmail_audit.py doctor
```

```
gmail-audit doctor

  python      3.12.4                                      ok
  gws         /usr/local/bin/gws                          ok
  auth        authenticated                               ok
  mailbox     35,012 messages, 21,004 threads             ok

Ready. Next:
    python gmail_audit.py engaged
```

One API call. It tells you which of the four setup failures you have, with
the `SETUP.md` section that fixes it — including the one `gws auth status`
misreports, where you are authenticated but the token carries no Gmail scope.
Worth thirty seconds before an hour-long scan.

The CLI below is the reference path. `python gmail_audit.py ui` puts steps 1
and 3 in a browser instead — an auth preflight that catches the scope trap
before you wait an hour for a scan that cannot work, and live scan progress
with the observed rate and rate-limit state. It reads and writes the same
cache, so you can mix the two freely. **It cannot trash anything**; reviewing
and trashing stay in the CLI until later phases land.

### 1. Baseline

```bash
python gmail_audit.py baseline
```

### 2. Build the engagement safeguard — do this first

```bash
python gmail_audit.py engaged
```

Scans `in:sent` for everyone you have written to. Senders on this list are
never recommended for Trash.

**Resumable.** Each scanned message is checkpointed to `engaged-cache.jsonl`,
so a run you kill (or that dies) picks up where it stopped instead of starting
over. An incomplete run deliberately writes **no** `engaged.txt`: every later
step only checks that the file exists, so a partial safeguard list would look
exactly like a complete one.

> Run this **before** ranking. Without it the safeguard is inactive and people
> you actively correspond with can be scored as Trash. This is enforced, not
> just warned about: `rank --review` and `trash` both **refuse** to run when
> `engaged.txt` is missing, and `engaged` refuses to write an empty list. If
> you genuinely have no sent mail, `--allow-missing-engaged` says so
> explicitly.

### 3. Fetch headers

```bash
python gmail_audit.py fetch --query in:inbox --concurrency 12
```

Oldest-first, resumable — re-running skips what is already cached. Roughly
20–25 minutes for 35,000 messages.

### 4. Rank

```bash
python gmail_audit.py rank
```

### 5. Review

```bash
python gmail_audit.py rank --review review.txt
```

The ranked index and the approval list are the same file, so there is nothing
to transcribe. Every row arrives **unmarked**; change the mark column on the
senders you want gone:

```
# mark flag sender                                    n  score  signals
t           news@deals.example.com                   60      8  List-Unsubscribe, ...
t           no-reply@sketchy.example.net             12      9  no-reply, SPF/DKIM fail
.     [!]   alerts@mybank.example.com                30      8  protected-domain, ...
.           jane@friend.example.com                   4      0
```

`[!]` is a safeguarded sender. Nothing pre-marks one, and trashing one costs a
second deliberate confirmation at execute time.

Re-running `rank --review` keeps the marks already in the file, so you can
review across several sittings, or fetch more mail part way through, without
losing a decision.

`--preselect-score N` pre-marks unguarded senders scoring at or above `N`. It
is off by default: the friction being removed is transcription, not judgement.

<details>
<summary>The older <code>--senders approved.txt</code> path still works</summary>

One address per line, `#` comments allowed. `trash --review` takes precedence
if both are given.
</details>

### 6. Dry run, then execute

```bash
python gmail_audit.py trash --review review.txt              # dry run
python gmail_audit.py trash --review review.txt --execute
```

Prompts between batches. Writes `trashed-manifest.jsonl` before touching
anything.

### Undo

```bash
python gmail_audit.py untrash --manifest trashed-manifest.jsonl --execute
```

Or empty Gmail's Trash yourself after 30 days if you're satisfied.

### Checking on a scan you walked away from

A full fetch takes tens of minutes. It publishes its progress to
`fetch-status.json` as it goes, so any other terminal can ask:

```bash
python gmail_audit.py status
```

```
fetch    running      18,450/35,012    52.7%   27.6 msg/s  eta 11m28s  drops 2
         limit 26.0/s ramping · query in:inbox · updated 1s ago · pid 4242
```

A scan that was killed reads as `STALE` rather than as permanently running,
because liveness comes from the file's timestamp. The UI reads the same file,
so the browser reports a scan started in a terminal.

---

## The local UI

```bash
python gmail_audit.py ui
```

Opens a browser on `http://127.0.0.1:8765`. Two panels: **preflight**, which
makes a real `getProfile` call and tells you whether you are unauthenticated or
authenticated-without-the-Gmail-scope — a distinction `gws auth status` gets
wrong; and **scan**, which runs the same `fetch` you would run from the CLI and
shows fetched/total, msg/s, ETA from observed throughput, drops, and what the
rate limiter is doing (`ramping`, `backoff 12s`, `at-max`).

It is deliberately small in what it can do:

- **Loopback only.** The bind address is asserted, not defaulted, and there is
  no flag to change it.
- **Token-gated.** A random token is generated per launch, carried in the URL
  the tool opens, never written to disk, and required on every request — the
  page included. `Host` and `Origin` are checked too, which is what stops DNS
  rebinding, and no CORS header is ever sent.
- **No deletion path.** There is no trash endpoint to reach. The security
  machinery above landed first, on a surface that cannot delete anything, so
  it is not the deletion path's first draft that gets tested.

It also reports a scan started from a terminal, by reading the same status
file `gmail_audit.py status` reads, and refuses to start a second one over it.

A review table in the browser and executing from the browser are both designed
but deferred, and deliberately deferred together — see
[docs/DESIGN-UI.md](docs/DESIGN-UI.md).

---

## Scoring

| Signal | Points |
|---|---|
| SPF / DKIM / DMARC failure | +3 |
| Volume ≥ 50 messages | +3 |
| `List-Unsubscribe` present | +2 |
| `Precedence: bulk\|list\|junk` | +2 |
| `no-reply@` style localpart | +2 |
| Volume ≥ 10 messages | +2 |
| `List-Id` present | +1 |
| Bulk-mailer `X-Mailer` (Mailchimp, SendGrid, …) | +1 |
| From/Reply-To domain mismatch | +1 |

**≥ 6 → Trash · 3–5 → Review · < 3 → Keep**

### Safeguards — always demote to Review, never Trash

- **replied-to** — the address appears in your sent mail
- **protected-domain** — banking, government, health, legal, education
- **starred/important** — any message from them is flagged

> Safeguards constrain the *ranking*, not your approved list. If you put a
> protected sender in `approved.txt`, it gets trashed. The guards inform your
> review; they do not override your decision.

### `Subject` is deliberately not scored

`Subject` is a header, but it is attacker-controlled free text — the same
injection surface as a message body. It is collected for clustering and the
audit trail, but **never contributes to a score**, and is truncated wherever
displayed.

---

## Rate limits

Gmail enforces quota **per minute**, not per second. `messages.get` costs 5
units.

**The tool paces itself.** One rate limiter, shared by every worker, ramps
until Gmail returns 429s, backs off and settles. The scan prints its current
rate and state (`ramping`, `backoff 4s`, `at-max`, ...) as it runs. You should
not normally need to tune anything; `--rate` pins it and `--max-rate` raises
the ceiling if your project has more quota.

Rate and concurrency are separate knobs now. The limiter governs the rate;
`--concurrency` only covers request latency, so about `rate * 0.35` workers are
needed to sustain a given rate.

**Never go above `--concurrency 16`.** Above that the API drops messages, which
undercounts senders and corrupts the ranking — a correctness problem, not a
speed one, and one the limiter does not repeal. Messages that cannot be fetched
are counted and written to `fetch-dropped.jsonl` for retry rather than
scrolling past. This measurement predates the limiter and is where 16 comes
from:

| Concurrency | Throughput | Result |
|---|---|---|
| 8 | 21.7 msg/s | clean |
| 16 | 35.7 msg/s | clean |
| 24 | 34.9 msg/s | clean |
| 32 | 44.6 msg/s | **27 of 120 dropped** |

## Platform notes

**PowerShell** strips inner double quotes when passing to a native `.exe`, so
`'{"userId":"me"}'` arrives as `{userId:me}`. Escape them:

```powershell
gws gmail users getProfile --params '{\"userId\":\"me\"}'
```

**Windows PATH** is stale in already-open shells after installing anything.
Restart the shell, or the tool will look missing when it isn't.

---

## What this tool will not do

- Read message bodies
- Permanently delete anything
- Act without an explicit list of senders you marked yourself
- Score based on `Subject`
- Listen on anything but loopback, or serve a request without the per-launch
  token

## Privacy

`headers.jsonl`, `engaged.txt`, `approved.txt` and the manifests contain real
sender addresses and message IDs from your mailbox. They are gitignored.
**Do not commit them.** Nothing is transmitted anywhere except to Google's own
API using your credentials.

## Roadmap

Shipped: the global rate limiter, preflight and live scan progress in the
browser, a status file that makes a scan walk-away-able, and the review file
that removes `approved.txt` transcription.

Next: incremental rescans via the History API, so a repeat audit takes seconds
rather than an hour.

Deferred: a review table in the browser, and executing from the browser. Both
are designed and neither is blocked; they wait because a browser execute path
is three layers deep and the selection model has already moved once. Building
them after the tool's shape settles costs no more and should need far fewer
full-stack passes. See [docs/DESIGN-UI.md](docs/DESIGN-UI.md), and
[docs/PLAN-RATE-LIMITER.md](docs/PLAN-RATE-LIMITER.md) for how the limiter
works.

## License

MIT — see [LICENSE](LICENSE).
