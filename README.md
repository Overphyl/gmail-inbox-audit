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
python tests/test_audit.py      # optional: 11 offline tests, no API access
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

> Run this **before** ranking. Without it the safeguard is inactive and people
> you actively correspond with can be scored as Trash. `rank` warns loudly if
> the list is missing, and `engaged` refuses to write an empty list.

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

### 5. Review, then approve

Read the ranked index and write the senders you want gone, one per line:

```
# approved.txt
news@deals.example.com
no-reply@sketchy.example.net
```

### 6. Dry run, then execute

```bash
python gmail_audit.py trash --senders approved.txt              # dry run
python gmail_audit.py trash --senders approved.txt --execute
```

Prompts between batches. Writes `trashed-manifest.jsonl` before touching
anything.

### Undo

```bash
python gmail_audit.py untrash --manifest trashed-manifest.jsonl --execute
```

Or empty Gmail's Trash yourself after 30 days if you're satisfied.

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
units. Measured against a real 35k mailbox:

| Concurrency | Throughput | Result |
|---|---|---|
| 8 | 21.7 msg/s | clean |
| 16 | 35.7 msg/s | clean |
| 24 | 34.9 msg/s | clean |
| 32 | 44.6 msg/s | **27 of 120 dropped** |

Stay at **12–16**. Above that the API drops messages, which undercounts senders
and corrupts the ranking — a correctness problem, not a speed one. The tool
retries with exponential backoff, but dropping concurrency is the real fix.

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
- Act without an explicit approved-sender list
- Score based on `Subject`

## Privacy

`headers.jsonl`, `engaged.txt`, `approved.txt` and the manifests contain real
sender addresses and message IDs from your mailbox. They are gitignored.
**Do not commit them.** Nothing is transmitted anywhere except to Google's own
API using your credentials.

## Roadmap

A browser-based UI is designed but not built: live scan progress, click-to-select
review instead of hand-editing `approved.txt`, and a global rate limiter to fix
sustained throughput. See [docs/DESIGN-UI.md](docs/DESIGN-UI.md).

## License

MIT — see [LICENSE](LICENSE).
