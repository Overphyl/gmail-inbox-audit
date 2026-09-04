# CLAUDE.md

Context for working on this repository.

## What this is

A CLI that audits a Gmail inbox by **message headers only**, ranks senders by
spam signals, and bulk-trashes senders the user has explicitly approved. Python
3.8+, **standard library only** — no dependencies, no virtualenv. It shells out
to the `gws` CLI for Gmail API access.

Everything lives in one module, `gmail_audit.py`, with subcommands: `baseline`,
`fetch`, `engaged`, `rank`, `trash`, `untrash`.

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

**Approval is a list, not a threshold.** `cmd_trash` refuses to run without an
explicit file of approved sender addresses. It must never act on "everything
scoring above N".

**Manifest before mutation.** `cmd_trash` writes every target message ID to
disk *before* trashing anything, so an interrupted run still leaves a complete
undo list for `cmd_untrash`.

**Safeguards demote, never promote.** Senders that are replied-to, on a
protected domain, or starred/important are forced to `Review` regardless of
score. They constrain the *ranking*; they deliberately do not override a
human's approved list.

## Layout

```
gmail_audit.py            the entire tool
docs/SETUP.md             OAuth setup, troubleshooting, platform notes
docs/DESIGN-UI.md         proposed web UI (not implemented)
docs/images/*.svg         hand-authored setup diagrams
tests/test_audit.py       11 offline tests, no API access needed
tests/fixtures/           synthetic headers, example.com domains only
tests/check_diagrams.py   geometric checks on the SVGs
```

### Where things are in `gmail_audit.py`

| Concern | Symbol |
|---|---|
| Subprocess wrapper, retry/backoff | `gws()` |
| Which headers get requested | `HEADERS`, `ENGAGED_HEADERS` |
| Message ID enumeration | `list_ids()` |
| Per-message header fetch | `get_headers()`, `_safe()` |
| Scan loop and concurrency | `cmd_fetch()` |
| Scoring | `score_sender()`, `BULK_MAILERS`, `NOREPLY`, `PROTECTED` |
| Ranking and safeguards | `cmd_rank()` |
| Mutation | `_trash_one()`, `cmd_trash()`, `cmd_untrash()` |

## Verifying changes

**Both suites run offline against fixtures. No Gmail access, no quota.** Use
them as the development loop rather than hitting a real mailbox.

```bash
python tests/test_audit.py
python tests/check_diagrams.py
```

`tests/fixtures/headers.jsonl` is synthetic and uses `example.com` domains
only. It deliberately includes senders that score in Trash range but must be
demoted — a bank, a replied-to vendor, a starred sender — so safeguard
regressions fail loudly.

When adding a signal or safeguard, add a fixture sender that exercises it. A
test that only passes because the fixture lacks the case is worse than none:
the `To`/`Cc` bug below shipped precisely because the fixture bypassed the API.

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
Short bursts reach ~35 msg/s at concurrency 16, but sustained over an hour the
observed rate collapses to ~5 msg/s, because per-request exponential backoff
idles each worker independently. Concurrency above ~16 causes the API to drop
messages outright, which silently undercounts senders and corrupts the ranking.
This is a correctness problem, not a performance one. See `docs/DESIGN-UI.md`
for the proposed shared-rate-limiter fix.

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
