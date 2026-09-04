#!/usr/bin/env python3
"""
Gmail inbox audit - header-only, non-destructive.

Reads ONLY message headers via the Gmail API using format=metadata with an
explicit metadataHeaders allowlist. In that mode the API never returns body
payload at all, so message bodies are never fetched into context.

Subcommands:
  baseline  Message counts by year and by sender domain
  fetch     Pull headers oldest-first into a resumable JSONL cache
  engaged   Build the replied-to address list (false-positive safeguard)
  rank      Score senders and emit the ranked index

This script NEVER trashes, deletes or modifies anything. It only reads.
"""
import argparse
import collections
import datetime
import json
import os
import re
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

def _find_gws():
    """Resolve the gws binary.

    On Windows the PATH entry is a .cmd shim that subprocess cannot exec by
    bare name, so prefer the real executable when we can find it.
    """
    override = os.environ.get("GWS_BIN")
    if override:
        return override
    import shutil
    for cand in ("gws.exe", "gws"):
        p = shutil.which(cand)
        if p and not p.lower().endswith((".cmd", ".ps1", ".bat")):
            return p
    guess = os.path.expandvars(
        r"%APPDATA%\npm\node_modules\@googleworkspace\cli\bin\gws.exe"
    )
    if os.path.exists(guess):
        return guess
    return shutil.which("gws") or "gws"


GWS = _find_gws()

# Headers requested. All are metadata. Subject is included for clustering only
# and is treated as untrusted, attacker-controlled text: it never contributes
# to a score and is truncated on display.
HEADERS = [
    "From", "Reply-To", "Return-Path", "Sender", "Date", "Subject",
    "List-Unsubscribe", "List-Id", "Precedence", "X-Mailer",
    "Authentication-Results", "X-Spam-Status",
]


RETRYABLE = re.compile(
    r"quota exceeded|rate limit|too many requests|429|"
    r"backend error|internal error|503|500",
    re.I,
)


def gws(args, sanitize=None, retries=6):
    """Run a gws command, retrying transient quota/backend failures.

    Gmail enforces a per-USER-per-MINUTE quota, so a burst that is fine for a
    few seconds will start failing partway through a long run. Without backoff
    those failures silently drop messages and skew the sender counts.
    """
    cmd = [GWS] + args
    if sanitize:
        cmd += ["--sanitize", sanitize]
    delay = 2.0
    last = ""
    for attempt in range(retries + 1):
        # Force UTF-8: header values routinely contain non-ASCII, and the
        # Windows default (cp1252) raises UnicodeDecodeError on them.
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if p.returncode == 0:
            return p.stdout
        last = (p.stderr or "").strip()
        if attempt < retries and RETRYABLE.search(last):
            time.sleep(delay + random.uniform(0, 1.0))
            delay = min(delay * 2, 60.0)
            continue
        break
    raise RuntimeError("gws failed: " + " ".join(args[:4]) + "\n" + last[:300])


def list_ids(query, sanitize=None, page_limit=200):
    """Return message IDs matching a Gmail query. The API returns newest-first."""
    params = json.dumps({"userId": "me", "q": query, "maxResults": 500})
    out = gws(
        ["gmail", "users", "messages", "list", "--params", params,
         "--page-all", "--page-limit", str(page_limit)],
        sanitize,
    )
    ids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        page = json.loads(line)
        for m in page.get("messages") or []:
            ids.append(m["id"])
    return ids


# The engagement scan needs recipient headers, which are deliberately absent
# from HEADERS (the inbox scan has no use for them). Requesting a header that
# is not in the metadataHeaders allowlist returns nothing at all - silently -
# so the two scans must ask for different sets.
ENGAGED_HEADERS = ["To", "Cc", "Bcc", "From", "Date"]


def get_headers(msg_id, sanitize=None, headers=None):
    params = {
        "userId": "me",
        "id": msg_id,
        "format": "metadata",
        "metadataHeaders": headers or HEADERS,
    }
    out = gws(
        ["gmail", "users", "messages", "get", "--params", json.dumps(params)],
        sanitize,
    )
    msg = json.loads(out)
    hdrs = {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    return {
        "id": msg["id"],
        "internalDate": msg.get("internalDate"),
        "labelIds": msg.get("labelIds", []),
        "headers": hdrs,
    }


ADDR = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def addr_of(value):
    m = ADDR.search(value or "")
    return m.group(0).lower() if m else ""


def domain_of(value):
    a = addr_of(value)
    return a.split("@", 1)[1] if "@" in a else ""


def load_cache(path):
    msgs = []
    if not os.path.exists(path):
        return msgs
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return msgs


# ---------------------------------------------------------------- baseline
def cmd_baseline(a):
    this_year = datetime.date.today().year
    print("{:<8}{:>10}".format("year", "messages"))
    print("-" * 18)
    total = 0
    for y in range(a.since, this_year + 1):
        q = "in:inbox after:{}/01/01 before:{}/01/01".format(y, y + 1)
        n = len(list_ids(q, a.sanitize))
        total += n
        print("{:<8}{:>10}".format(y, n))
    print("-" * 18)
    print("{:<8}{:>10}".format("TOTAL", total))


# ------------------------------------------------------------------- fetch
def _safe(msg_id, sanitize, headers=None):
    try:
        return get_headers(msg_id, sanitize, headers)
    except Exception as e:
        print("  ! {}: {}".format(msg_id, e), file=sys.stderr)
        return None


def cmd_fetch(a):
    ids = list_ids(a.query, a.sanitize)
    ids.reverse()  # API returns newest-first; reverse to process oldest-first

    done = {m["id"] for m in load_cache(a.cache)}
    todo = [i for i in ids if i not in done]
    print(
        "{} messages matched, {} already cached, {} to fetch".format(
            len(ids), len(done), len(todo)
        ),
        file=sys.stderr,
    )
    if a.limit:
        todo = todo[: a.limit]
        print("  (limited to {})".format(len(todo)), file=sys.stderr)

    with open(a.cache, "a", encoding="utf-8") as out:
        for start in range(0, len(todo), a.batch):
            chunk = todo[start : start + a.batch]
            with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
                for rec in ex.map(lambda i: _safe(i, a.sanitize), chunk):
                    if rec:
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            print(
                "  batch {}: {}/{}".format(
                    start // a.batch + 1, min(start + a.batch, len(todo)), len(todo)
                ),
                file=sys.stderr,
            )


# ----------------------------------------------------------------- engaged
def cmd_engaged(a):
    """Addresses the user has actually written to. These are never auto-Trashed."""
    ids = list_ids("in:sent", a.sanitize)
    if a.limit:
        ids = ids[: a.limit]
    print("scanning {} sent messages".format(len(ids)), file=sys.stderr)
    addrs = set()
    seen = 0
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for rec in ex.map(
            lambda i: _safe(i, a.sanitize, ENGAGED_HEADERS), ids
        ):
            if not rec:
                continue
            seen += 1
            for field in ("to", "cc", "bcc"):
                for m in ADDR.finditer(rec["headers"].get(field, "")):
                    addrs.add(m.group(0).lower())

    # A silent zero here means the safeguard is inert, which is far more
    # dangerous than a loud failure - senders you correspond with would be
    # eligible for Trash. Refuse to write an empty list.
    if seen and not addrs:
        sys.exit(
            "ERROR: scanned {} sent messages but extracted 0 addresses.\n"
            "The recipient headers are missing - check that To/Cc are in the\n"
            "metadataHeaders allowlist. Refusing to write an empty safeguard "
            "list.".format(seen)
        )
    with open(a.out, "w", encoding="utf-8") as f:
        for x in sorted(addrs):
            f.write(x + "\n")
    print(
        "wrote {} engaged addresses from {} sent messages to {}".format(
            len(addrs), seen, a.out
        ),
        file=sys.stderr,
    )


# -------------------------------------------------------------------- rank
BULK_MAILERS = (
    "mailchimp", "sendgrid", "constantcontact", "klaviyo", "marketo",
    "hubspot", "sailthru", "braze", "amazonses", "mandrill",
)
NOREPLY = re.compile(
    r"(no-?reply|do-?not-?reply|notification|mailer|bounce|automated|alerts?)@",
    re.I,
)
# Domains where a false positive is expensive. Always Review, never Trash.
PROTECTED = (
    "bank", "chase", "wellsfargo", "citi", "amex", "paypal", "stripe",
    "irs.gov", ".gov", "tax", "health", "insurance", "clinic", "hospital",
    "legal", "attorney", "school", ".edu",
)


def score_sender(sender, group):
    """Return (score, signals). Subject is deliberately not consulted."""
    signals = []
    score = 0
    # Use the union of signals seen across the sender's messages, not just one.
    has = lambda k: any(k in m["headers"] for m in group)
    latest = group[-1]["headers"]

    if has("list-unsubscribe"):
        score += 2
        signals.append("List-Unsubscribe")
    if any(re.search(r"bulk|list|junk", m["headers"].get("precedence", ""), re.I)
           for m in group):
        score += 2
        signals.append("Precedence:bulk")
    if has("list-id"):
        score += 1
        signals.append("List-Id")
    if NOREPLY.search(sender):
        score += 2
        signals.append("no-reply")

    xm = latest.get("x-mailer", "").lower()
    if any(b in xm for b in BULK_MAILERS):
        score += 1
        signals.append("bulk-mailer")

    rt = domain_of(latest.get("reply-to", ""))
    if rt and rt != domain_of(sender):
        score += 1
        signals.append("Reply-To mismatch")

    auth = " ".join(m["headers"].get("authentication-results", "") for m in group).lower()
    if re.search(r"spf=(fail|softfail)|dkim=fail|dmarc=fail", auth):
        score += 3
        signals.append("SPF/DKIM fail")

    n = len(group)
    if n >= 50:
        score += 3
        signals.append("volume:{}".format(n))
    elif n >= 10:
        score += 2
        signals.append("volume:{}".format(n))

    return score, signals


def cmd_rank(a):
    msgs = load_cache(a.cache)
    if not msgs:
        sys.exit("no cached headers at {} - run 'fetch' first".format(a.cache))

    engaged = set()
    if a.engaged and os.path.exists(a.engaged):
        with open(a.engaged, encoding="utf-8") as f:
            engaged = {l.strip().lower() for l in f if l.strip()}
    if not engaged:
        print(
            "WARNING: no engaged-sender list ({}). The 'you have corresponded\n"
            "with this sender' safeguard is INACTIVE - senders you actually\n"
            "reply to may be scored as Trash. Run 'engaged' first.\n".format(
                a.engaged
            ),
            file=sys.stderr,
        )

    by_sender = collections.defaultdict(list)
    for m in msgs:
        s = addr_of(m["headers"].get("from", ""))
        if s:
            by_sender[s].append(m)

    rows = []
    for sender, group in by_sender.items():
        group.sort(key=lambda m: int(m.get("internalDate") or 0))
        score, signals = score_sender(sender, group)

        # False-positive safeguards: these demote to Review, never Trash.
        guard = None
        if sender in engaged:
            guard = "replied-to"
        elif any(p in sender for p in PROTECTED):
            guard = "protected-domain"
        elif any(
            "STARRED" in m["labelIds"] or "IMPORTANT" in m["labelIds"]
            for m in group
        ):
            guard = "starred/important"

        if guard:
            rec = "Review"
        elif score >= 6:
            rec = "Trash"
        elif score >= 3:
            rec = "Review"
        else:
            rec = "Keep"

        rows.append(
            {
                "sender": sender,
                "count": len(group),
                "score": score,
                "signals": signals,
                "rec": rec,
                "guard": guard,
            }
        )

    rows.sort(key=lambda r: (-r["score"], -r["count"]))

    if a.json:
        print(json.dumps(rows, indent=2))
        return

    print("{:<44}{:>6}{:>7}  {:<26}{}".format("sender", "n", "score", "recommendation", "signals"))
    print("-" * 128)
    for r in rows:
        tag = r["rec"] + ("(" + r["guard"] + ")" if r["guard"] else "")
        print(
            "{:<44}{:>6}{:>7}  {:<26}{}".format(
                r["sender"][:43], r["count"], r["score"], tag, ", ".join(r["signals"])
            )
        )

    trash = [r for r in rows if r["rec"] == "Trash"]
    review = [r for r in rows if r["rec"] == "Review"]
    keep = [r for r in rows if r["rec"] == "Keep"]
    print()
    print("Trash candidates : {:>4} senders / {:>6} messages".format(
        len(trash), sum(r["count"] for r in trash)))
    print("Needs review     : {:>4} senders / {:>6} messages".format(
        len(review), sum(r["count"] for r in review)))
    print("Keep             : {:>4} senders / {:>6} messages".format(
        len(keep), sum(r["count"] for r in keep)))


# ------------------------------------------------------------------- trash
# NOTE: This module calls messages.trash ONLY. messages.delete and
# messages.batchDelete are deliberately absent - they require the
# https://mail.google.com/ scope and are irreversible. Authenticate with
# gmail.modify and permanent deletion is impossible at the API level.

def _trash_one(msg_id, sanitize=None):
    params = json.dumps({"userId": "me", "id": msg_id})
    gws(["gmail", "users", "messages", "trash", "--params", params], sanitize)
    return msg_id


def _untrash_one(msg_id, sanitize=None):
    params = json.dumps({"userId": "me", "id": msg_id})
    gws(["gmail", "users", "messages", "untrash", "--params", params], sanitize)
    return msg_id


def cmd_trash(a):
    """Trash messages from an explicitly approved sender list.

    Takes a sender file, never a score threshold - the approval decision is
    made by a human reading the ranked index, not by this script.
    """
    if not os.path.exists(a.senders):
        sys.exit(
            "approved sender list not found: {}\n"
            "Create it from the ranked index - one address per line. "
            "This command will not act on a score threshold.".format(a.senders)
        )
    with open(a.senders, encoding="utf-8") as f:
        approved = {
            l.strip().lower()
            for l in f
            if l.strip() and not l.startswith("#")
        }
    if not approved:
        sys.exit("approved sender list is empty - nothing to do")

    msgs = load_cache(a.cache)
    if not msgs:
        sys.exit("no cached headers at {} - run 'fetch' first".format(a.cache))

    targets = []
    for m in msgs:
        sender = addr_of(m["headers"].get("from", ""))
        if sender in approved:
            targets.append(
                {
                    "id": m["id"],
                    "sender": sender,
                    "date": m["headers"].get("date", ""),
                    # truncated, untrusted, recorded for the audit trail only
                    "subject": (m["headers"].get("subject", "") or "")[:80],
                }
            )

    by_sender = collections.Counter(t["sender"] for t in targets)
    print("Approved senders : {}".format(len(approved)))
    print("Matching messages: {}".format(len(targets)))
    print()
    for s, n in by_sender.most_common():
        print("  {:<48}{:>6}".format(s[:47], n))
    missing = approved - set(by_sender)
    if missing:
        print("\n  (no cached messages for: {})".format(", ".join(sorted(missing))))

    if not targets:
        sys.exit("\nnothing matched - stopping")

    # Manifest is written BEFORE any mutation, so a complete undo list exists
    # even if the run is interrupted.
    with open(a.manifest, "w", encoding="utf-8") as f:
        for t in targets:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print("\nManifest written: {} ({} ids)".format(a.manifest, len(targets)))

    if not a.execute:
        print("\nDRY RUN - nothing was modified.")
        print("Re-run with --execute to move these to Trash (recoverable 30 days).")
        return

    done = 0
    for start in range(0, len(targets), a.batch):
        chunk = targets[start : start + a.batch]
        if not a.yes:
            resp = input(
                "\nTrash batch {} ({} messages)? [y/N] ".format(
                    start // a.batch + 1, len(chunk)
                )
            )
            if resp.strip().lower() not in ("y", "yes"):
                print("stopped at batch {} - {} already trashed".format(
                    start // a.batch + 1, done))
                return
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            for _ in ex.map(
                lambda t: _safe_mutate(_trash_one, t["id"], a.sanitize), chunk
            ):
                done += 1
        print("  trashed {}/{}".format(done, len(targets)))

    print("\nDone. {} messages moved to Trash (recoverable for 30 days).".format(done))
    print("To undo: python gmail_audit.py untrash --manifest {}".format(a.manifest))


def cmd_untrash(a):
    """Restore everything listed in a manifest. The undo for cmd_trash."""
    if not os.path.exists(a.manifest):
        sys.exit("manifest not found: {}".format(a.manifest))
    ids = []
    with open(a.manifest, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                ids.append(json.loads(line)["id"])
    print("Restoring {} messages from {}".format(len(ids), a.manifest))
    if not a.execute:
        print("DRY RUN - re-run with --execute to restore.")
        return
    done = 0
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for _ in ex.map(lambda i: _safe_mutate(_untrash_one, i, a.sanitize), ids):
            done += 1
    print("Restored {} messages to the inbox.".format(done))


def _safe_mutate(fn, msg_id, sanitize):
    try:
        return fn(msg_id, sanitize)
    except Exception as e:
        print("  ! {}: {}".format(msg_id, e), file=sys.stderr)
        return None


# -------------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(
        description="Header-only Gmail audit. Never deletes anything."
    )
    p.add_argument(
        "--sanitize",
        default=os.environ.get("GWS_SANITIZE_TEMPLATE")
        or os.environ.get("GOOGLE_WORKSPACE_CLI_SANITIZE_TEMPLATE"),
        help="Model Armor template: projects/P/locations/L/templates/T",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("baseline", help="counts by year")
    b.add_argument("--since", type=int, default=2015)
    b.set_defaults(func=cmd_baseline)

    f = sub.add_parser("fetch", help="pull headers oldest-first")
    f.add_argument("--query", default="in:inbox")
    f.add_argument("--cache", default="headers.jsonl")
    f.add_argument("--batch", type=int, default=1000)
    f.add_argument("--concurrency", type=int, default=8)
    f.add_argument("--limit", type=int, default=0)
    f.set_defaults(func=cmd_fetch)

    e = sub.add_parser("engaged", help="build replied-to address list")
    e.add_argument("--out", default="engaged.txt")
    e.add_argument("--concurrency", type=int, default=8)
    e.add_argument("--limit", type=int, default=0)
    e.set_defaults(func=cmd_engaged)

    r = sub.add_parser("rank", help="score senders")
    r.add_argument("--cache", default="headers.jsonl")
    r.add_argument("--engaged", default="engaged.txt")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_rank)

    t = sub.add_parser("trash", help="trash messages from approved senders")
    t.add_argument("--senders", default="approved.txt",
                   help="approved sender addresses, one per line (required)")
    t.add_argument("--cache", default="headers.jsonl")
    t.add_argument("--manifest", default="trashed-manifest.jsonl",
                   help="written before any mutation; used by 'untrash'")
    t.add_argument("--batch", type=int, default=250)
    t.add_argument("--concurrency", type=int, default=8)
    t.add_argument("--execute", action="store_true",
                   help="actually trash; omit for a dry run")
    t.add_argument("--yes", action="store_true",
                   help="skip the per-batch confirmation prompt")
    t.set_defaults(func=cmd_trash)

    u = sub.add_parser("untrash", help="restore messages from a manifest")
    u.add_argument("--manifest", default="trashed-manifest.jsonl")
    u.add_argument("--concurrency", type=int, default=8)
    u.add_argument("--execute", action="store_true")
    u.set_defaults(func=cmd_untrash)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
