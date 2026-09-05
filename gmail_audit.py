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
import threading
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


# ----------------------------------------------------------- rate limiting
# Gmail enforces a per-USER-per-MINUTE quota. Before this existed, every
# worker owned a private backoff `delay` and slept alone on discovering the
# ceiling, so twelve workers spent most of their wall clock asleep and a
# sustained scan collapsed to ~5 msg/s. The fix is one shared limiter that
# paces the whole fleet, plus AIMD so the unqueryable ceiling is re-found
# rather than guessed.

# Two failure modes that call for opposite responses. THROTTLE means the
# FLEET is too fast: shrink the shared rate and re-queue with no local sleep.
# TRANSIENT means THIS REQUEST failed: back off locally and leave the rate
# alone. Word boundaries matter - Gmail message IDs are lowercase hex, so a
# bare 429/500/503 alternative can match an ID inside an error string and
# treat a hard failure as retryable, burning six retries on it.
THROTTLE = re.compile(
    r"quota exceeded|rate[ _-]?limit|too many requests|\b429\b|"
    r"userRateLimitExceeded|rateLimitExceeded",
    re.I,
)
TRANSIENT = re.compile(
    r"backend error|internal error|service unavailable|deadline exceeded|"
    r"\b50[03]\b",
    re.I,
)


def _retryable(stderr):
    """True if this stderr is worth another attempt at all."""
    s = stderr or ""
    return bool(THROTTLE.search(s) or TRANSIENT.search(s))


RATE_START = 8.0         # req/s at launch
RATE_MIN = 1.0           # never reach zero; also bounds Ctrl-C latency
RATE_MAX = 40.0          # between the last clean run (35.7/s) and the first
                         # that dropped messages (44.6/s)
RATE_BURST = 4           # requests admitted instantaneously
RAMP_STEP = 1.0          # additive increase, req/s
RAMP_INTERVAL = 3.0      # ...at most this often
THROTTLE_FACTOR = 0.7    # multiplicative decrease
THROTTLE_PAUSE = 1.0     # fleet-wide pause applied on a throttle, seconds
THROTTLE_HOLD = 15.0     # no ramping for this long after a decrease
THROTTLE_COALESCE = 2.0  # throttles this close together decrease the rate once


class RateLimiter:
    """A shared, adaptive pacer for the whole worker fleet.

    A token bucket in GCRA form: one scalar theoretical-arrival-time plus a
    burst tolerance, guarded by a plain Lock. The deadline formulation is the
    design, not an implementation detail - it prevents a thundering herd
    rather than dispersing one. Token accrual is a pure function of wall
    time, so a caller can atomically CLAIM the next departure slot under the
    lock and then sleep alone until its own private instant. No waiter ever
    wakes to find its slot taken, so the herd cannot form.

    reserve() is a pure state transition over an injected clock and never
    sleeps; acquire() is reserve() plus the wait. That split is what makes
    fleet behaviour testable with no threads at all.
    """

    def __init__(self, rate=RATE_START, burst=RATE_BURST, min_rate=RATE_MIN,
                 max_rate=RATE_MAX, adaptive=True, clock=time.monotonic,
                 sleeper=time.sleep):
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._min = float(min_rate)
        self._max = max(float(max_rate), self._min)
        self._rate = max(self._min, min(float(rate), self._max))
        self._burst = max(1, int(burst))
        self.adaptive = bool(adaptive)
        now = clock()
        self._tat = now                 # theoretical arrival time
        self._hold_until = 0.0          # no ramping before this instant
        self._last_increase = now
        self._last_decrease = float("-inf")
        # Guard 1: only raise the rate when the limiter is actually binding.
        # If nobody waited, the constraint is --concurrency or Gmail latency,
        # and raising anyway builds unearned credit, spent later as an
        # overshoot burst the moment latency improves.
        self._waited_since_probe = False
        self._stopped = False
        self._throttles = 0
        self._server_errors = 0
        self._errors = 0
        self._grants = 0
        self._waits = 0

    # -------------------------------------------------------------- pacing
    @property
    def rate(self):
        with self._lock:
            return self._rate

    def reserve(self):
        """Claim the next departure slot. Returns the instant to depart."""
        with self._lock:
            now = self._clock()
            interval = 1.0 / self._rate
            # Tolerance is (B-1)/R, not B/R: with burst B the Bth request
            # still departs immediately and the (B+1)th waits one interval.
            tolerance = (self._burst - 1) * interval
            tat = self._tat if self._tat > now else now
            deadline = tat - tolerance
            self._tat = tat + interval
            self._grants += 1
            if deadline > now:
                self._waited_since_probe = True
                self._waits += 1
            else:
                deadline = now
            self._maybe_increase(now)
            return deadline

    def acquire(self):
        """reserve(), then sleep until the deadline in <=1s slices.

        Short slices are what make shutdown() observable: a worker parked at
        the rate floor would otherwise be uninterruptible for many seconds.
        """
        deadline = self.reserve()
        while True:
            with self._lock:
                if self._stopped:
                    return
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            self._sleeper(remaining if remaining < 1.0 else 1.0)

    def shutdown(self):
        """Stop pacing. Used on Ctrl-C so workers drain instead of sleeping."""
        with self._lock:
            self._stopped = True

    # ---------------------------------------------------------- adaptation
    def _maybe_increase(self, now):
        """Additive increase. The caller holds the lock."""
        if not self.adaptive:
            return
        if now < self._hold_until:
            return
        if now - self._last_increase < RAMP_INTERVAL:
            return
        if not self._waited_since_probe:
            return  # guard 1: not binding, so there is nothing to earn
        self._rate = min(self._max, self._rate + RAMP_STEP)
        self._last_increase = now
        self._waited_since_probe = False

    def on_success(self):
        pass

    def on_throttle(self):
        """The fleet is too fast. Pause everyone, then shrink the rate once."""
        with self._lock:
            now = self._clock()
            self._throttles += 1
            # This delays only FUTURE acquirers. Workers already holding a
            # deadline proceed, so overshoot is bounded by --concurrency
            # requests. Revoking issued deadlines would need a Condition and
            # buy nothing.
            pause = now + THROTTLE_PAUSE
            if pause > self._tat:
                self._tat = pause
            if not self.adaptive:
                return  # a pinned rate still pauses, but never shrinks
            # Guard 3: over the ceiling, the API throttles most of the ~12
            # in-flight workers within a few hundred milliseconds. A naive
            # per-event decrease gives 0.7^12 = 0.014 - 35 req/s down to 0.5
            # from one overshoot. Extra throttles are still counted for
            # reporting; they simply do not compound.
            if now - self._last_decrease < THROTTLE_COALESCE:
                return
            self._rate = max(self._min, self._rate * THROTTLE_FACTOR)
            self._last_decrease = now
            # Guard 2: hold, bounding the sawtooth period and giving the
            # API's own averaging window time to drain.
            self._hold_until = now + THROTTLE_HOLD
            self._waited_since_probe = False

    def on_server_error(self):
        """A 5xx. This request failed; the rate is not implicated."""
        with self._lock:
            self._server_errors += 1

    def on_error(self):
        with self._lock:
            self._errors += 1

    # ----------------------------------------------------------- reporting
    def state(self):
        with self._lock:
            return self._state(self._clock())

    def _state(self, now):
        """The caller holds the lock."""
        if not self.adaptive:
            return "pinned"
        if self._rate <= self._min + 1e-9:
            # Capitalised deliberately: a limiter pinned at the minimum means
            # something is badly wrong and should not look routine.
            return "FLOOR"
        if now < self._hold_until:
            return "backoff {}s".format(int(self._hold_until - now) + 1)
        if self._rate >= self._max - 1e-9:
            return "at-max"
        return "ramping" if self._waited_since_probe else "holding"

    def stats(self):
        """A plain dict, so a later /api/progress can serve it in-process."""
        with self._lock:
            return {
                "rate": self._rate,
                "min_rate": self._min,
                "max_rate": self._max,
                "burst": self._burst,
                "adaptive": self.adaptive,
                "state": self._state(self._clock()),
                "throttles": self._throttles,
                "server_errors": self._server_errors,
                "errors": self._errors,
                "grants": self._grants,
                "waits": self._waits,
            }


# The quota is per-process-per-user, so two limiters in one process is a bug,
# not a configuration. This is a module handle for the same reason GWS is:
# resolved once in main(), overridden per call only by tests. It also has to
# outlive cmd_fetch's per-batch ThreadPoolExecutor - per-pool state would
# reset the learned rate and re-ramp from 8 req/s about 35 times over a full
# inbox.
LIMITER = None

# The live scan counters, for the same reason and the same future consumer.
PROGRESS = None


# Seams. Both exist so tests can replace them by assignment, with no mocking
# library: replacing _run alone gives full offline control of a whole cmd_*.
_sleep = time.sleep


def _run(cmd):
    """The single place this module spawns a subprocess."""
    # Force UTF-8: header values routinely contain non-ASCII, and the
    # Windows default (cp1252) raises UnicodeDecodeError on them.
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def gws(args, sanitize=None, retries=6, throttle_retries=12, limiter=None):
    """Run a gws command, paced by the shared limiter.

    Two retry budgets, because the two failure modes are different. Throttles
    get throttle_retries with NO local sleep - limiter.acquire() is the
    backoff now, and reusing a budget of 6 would burn through in about six
    seconds and drop the message. Transient 5xx keep the original per-request
    exponential backoff, which does not touch the shared rate.
    """
    cmd = [GWS] + args
    if sanitize:
        cmd += ["--sanitize", sanitize]
    lim = LIMITER if limiter is None else limiter
    delay = 2.0
    last = ""
    transient_left = retries
    throttle_left = throttle_retries
    while True:
        if lim is not None:
            lim.acquire()
        p = _run(cmd)
        if p.returncode == 0:
            if lim is not None:
                lim.on_success()
            return p.stdout
        last = (p.stderr or "").strip()
        if THROTTLE.search(last):
            if lim is not None:
                lim.on_throttle()
                if throttle_left > 0:
                    throttle_left -= 1
                    continue  # no local sleep; the limiter is the backoff
            elif transient_left > 0:
                # --no-rate-limit: nothing else would slow us down, so fall
                # back to the pre-limiter per-request backoff.
                transient_left -= 1
                _sleep(delay + random.uniform(0, 1.0))
                delay = min(delay * 2, 60.0)
                continue
        elif TRANSIENT.search(last):
            if lim is not None:
                lim.on_server_error()
            if transient_left > 0:
                transient_left -= 1
                _sleep(delay + random.uniform(0, 1.0))
                delay = min(delay * 2, 60.0)
                continue
        elif lim is not None:
            lim.on_error()
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
def _safe(msg_id, sanitize, headers=None, on_drop=None):
    """Fetch one message's headers, or None. Never raises.

    on_drop(msg_id, exc) takes over reporting when given, so the caller can
    count the drop, record it for retry and suppress a flood. With no
    callback the behaviour is exactly what it always was.
    """
    try:
        return get_headers(msg_id, sanitize, headers)
    except Exception as e:
        if on_drop is None:
            print("  ! {}: {}".format(msg_id, e), file=sys.stderr)
        else:
            on_drop(msg_id, e)
        return None


LATENCY = 0.35  # observed mean messages.get round trip, seconds

# The .jsonl extension is a safety requirement, not a style choice: these
# files hold real message IDs, the same class of data as headers.jsonl, and
# .gitignore carries a bare *.jsonl rule. Renaming either to .txt would
# silently make it committable.
FETCH_DROPPED = "fetch-dropped.jsonl"
ENGAGED_DROPPED = "engaged-dropped.jsonl"


class FetchProgress:
    """Live counters for a scan.

    snapshot() returns a plain dict, which is the one forward commitment to
    the web UI: a later cmd_ui() can serve /api/progress in-process without
    writing a file.
    """

    WINDOW = 10.0        # seconds of history behind the instantaneous rate
    MAX_DROP_LINES = 20  # per-message failure lines before suppression
    ABORT_AFTER = 25     # consecutive non-retryable failures

    def __init__(self, total, drop_path=None):
        self.total = int(total)
        self.done = 0
        self.dropped = 0
        self.aborted = None      # set to a reason string once we give up
        self.started = time.monotonic()
        self.drop_path = drop_path or None
        self._recent = collections.deque()
        self._lock = threading.Lock()
        self._drop_file = None
        self._drop_lines = 0
        self._consecutive = 0

    # ------------------------------------------------------------ counters
    def record_done(self, n=1):
        with self._lock:
            self.done += n
            now = time.monotonic()
            self._recent.append(now)
            cutoff = now - self.WINDOW
            while self._recent and self._recent[0] < cutoff:
                self._recent.popleft()
            self._consecutive = 0

    def record_drop(self, msg_id, error):
        """Count a message this run could not fetch, and write it down.

        The file names what to retry; it does not archive content. No
        headers, no subject - the same class of data as headers.jsonl, and
        the .jsonl extension is what keeps it gitignored.
        """
        text = str(error).replace("\n", " ")[:200]
        with self._lock:
            self.dropped += 1
            if self.drop_path:
                if self._drop_file is None:
                    # Lazily, so a clean run leaves no file at all.
                    self._drop_file = open(self.drop_path, "a", encoding="utf-8")
                self._drop_file.write(
                    json.dumps(
                        {
                            "id": msg_id,
                            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                            "error": text,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                # Flushed per line: an interrupted run is exactly when this
                # file matters.
                self._drop_file.flush()
            if self._drop_lines < self.MAX_DROP_LINES:
                self._drop_lines += 1
                print("  ! {}: {}".format(msg_id, text), file=sys.stderr)
                if self._drop_lines == self.MAX_DROP_LINES:
                    print(
                        "  ! (further failures suppressed; {} has them all)".format(
                            self.drop_path or "the counters below"
                        ),
                        file=sys.stderr,
                    )
            # An expired refresh token matches neither regex, so without this
            # the loop would churn through every remaining ID at full speed
            # and write a 30,000-line drop file. SETUP.md documents that
            # External/Testing tokens expire after seven days, so a long run
            # really can lose auth mid-flight.
            if not _retryable(text):
                self._consecutive += 1
                if self._consecutive >= self.ABORT_AFTER and not self.aborted:
                    self.aborted = "{} consecutive non-retryable failures".format(
                        self._consecutive
                    )
            else:
                self._consecutive = 0

    def abort(self, reason):
        with self._lock:
            if not self.aborted:
                self.aborted = reason

    def close(self):
        with self._lock:
            if self._drop_file is not None:
                self._drop_file.close()
                self._drop_file = None

    # ----------------------------------------------------------- reporting
    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            elapsed = max(now - self.started, 1e-9)
            cutoff = now - self.WINDOW
            while self._recent and self._recent[0] < cutoff:
                self._recent.popleft()
            span = min(elapsed, self.WINDOW)
            inst = len(self._recent) / span if span > 0 else 0.0
            avg = self.done / elapsed
            left = max(self.total - self.done - self.dropped, 0)
            return {
                "total": self.total,
                "done": self.done,
                "dropped": self.dropped,
                "elapsed": elapsed,
                "rate": inst,
                "avg_rate": avg,
                "eta": (left / avg) if avg > 0 and left else 0.0,
                "aborted": self.aborted,
            }


def _fmt_eta(seconds):
    if seconds <= 0:
        return "--"
    s = int(seconds)
    if s >= 3600:
        return "{}h{:02d}m".format(s // 3600, (s % 3600) // 60)
    if s >= 60:
        return "{}m{:02d}s".format(s // 60, s % 60)
    return "{}s".format(s)


def _progress_line(progress, limiter):
    p = progress.snapshot()
    pct = (100.0 * p["done"] / p["total"]) if p["total"] else 100.0
    st = limiter.stats() if limiter is not None else None
    if st is None:
        pace = "limit off"
    else:
        pace = "limit {:.1f}/s {:<11}".format(st["rate"], st["state"])
    total = "{:,}".format(p["total"])
    # Right-align done to the width of total, so the line does not jitter
    # sideways as the counter grows.
    line = "  {:>{w}}/{}  {:4.1f}%  {:5.1f} msg/s (avg {:.1f})  {} eta {}  drops {}".format(
        "{:,}".format(p["done"]), total, pct, p["rate"], p["avg_rate"], pace,
        _fmt_eta(p["eta"]), p["dropped"], w=len(total),
    )
    if st and st["throttles"]:
        line += "  thr {}".format(st["throttles"])
    return line


def _progress_reporter(progress, limiter, stop, interval=2.0, plain_every=10.0):
    """Print the live line from its own thread.

    It has to be a thread rather than a print inside the ex.map consumer:
    printing from the consumer freezes during a global pause, which is the
    exact pathology this phase fixes. A stall should read as "backoff 12s",
    not as a frozen counter.
    """
    try:
        tty = sys.stderr.isatty()
    except Exception:
        tty = False
    width = 0
    last_plain = 0.0
    while True:
        stopping = stop.wait(interval)
        line = _progress_line(progress, limiter)
        if tty:
            sys.stderr.write("\r" + line.ljust(width))
            sys.stderr.flush()
            width = max(width, len(line))
        else:
            now = time.monotonic()
            if stopping or now - last_plain >= plain_every:
                last_plain = now
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
        if stopping:
            if tty:
                sys.stderr.write("\n")
                sys.stderr.flush()
            return


def _pacing_note(a):
    """Describe the pacing at start, so the rate/concurrency coupling shows."""
    workers = getattr(a, "concurrency", 0)
    ceiling = workers / LATENCY if workers else 0.0
    if LIMITER is None:
        return "  pacing: rate limiter DISABLED; {} workers imply <= {:.0f} req/s".format(
            workers, ceiling
        )
    st = LIMITER.stats()
    if not st["adaptive"]:
        how = "pinned at {:.1f} req/s".format(st["rate"])
    else:
        how = "adaptive from {:.1f} to {:.1f} req/s".format(st["rate"], st["max_rate"])
    return "  pacing: {}; {} workers imply <= {:.0f} req/s".format(how, workers, ceiling)


def _scan(ids, a, progress, headers=None):
    """Fetch headers for ids, yielding records in order and counting drops."""

    def one(msg_id):
        if progress.aborted:
            return None  # drain fast rather than joining a doomed pool slowly
        return _safe(msg_id, a.sanitize, headers, on_drop=progress.record_drop)

    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        try:
            for rec in ex.map(one, ids):
                if rec:
                    progress.record_done()
                yield rec
        except KeyboardInterrupt:
            # Before the executor joins, not after: a worker parked in
            # acquire() would otherwise hold the join for seconds.
            if LIMITER is not None:
                LIMITER.shutdown()
            progress.abort("interrupted")
            raise


def _with_reporter(progress):
    """Start the reporter thread; returns (stop_event, thread)."""
    stop = threading.Event()
    t = threading.Thread(
        target=_progress_reporter, args=(progress, LIMITER, stop)
    )
    t.daemon = True
    t.start()
    return stop, t


def _report_drops(progress, total, label):
    """The end-of-scan reconciliation. Printed even on a clean run."""
    if not progress.dropped:
        return
    print(file=sys.stderr)
    print(
        "WARNING: {:,} of {:,} messages could not be fetched after retries.".format(
            progress.dropped, total
        ),
        file=sys.stderr,
    )
    print(
        "  Sender counts from this run are INCOMPLETE - the ranking will "
        "undercount.",
        file=sys.stderr,
    )
    if progress.drop_path:
        print("  IDs written to: {}".format(progress.drop_path), file=sys.stderr)
    print(
        "  Re-run the same {} command to retry them; the cache resumes by "
        "diffing\n  IDs, so nothing already fetched is re-requested.".format(label),
        file=sys.stderr,
    )


def _report_abort(progress):
    if not progress.aborted or progress.aborted == "interrupted":
        return
    sys.exit(
        "\nABORTED: {}.\n"
        "This usually means the OAuth refresh token expired mid-run - an\n"
        "External/Testing token lasts seven days. Re-authenticate and re-run;\n"
        "the cache resumes where it stopped:\n"
        "    gws auth login".format(progress.aborted)
    )


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
    print(_pacing_note(a), file=sys.stderr)

    global PROGRESS
    progress = FetchProgress(len(todo), drop_path=getattr(a, "dropped", "") or None)
    PROGRESS = progress
    a.progress = progress

    interrupted = False
    stop, reporter = _with_reporter(progress)
    try:
        with open(a.cache, "a", encoding="utf-8") as out:
            for start in range(0, len(todo), a.batch):
                chunk = todo[start : start + a.batch]
                for rec in _scan(chunk, a, progress):
                    if rec:
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                # A coarse checkpoint that survives the carriage-return
                # overwriting above.
                print(
                    "  batch {}: {}/{}".format(
                        start // a.batch + 1,
                        min(start + a.batch, len(todo)),
                        len(todo),
                    ),
                    file=sys.stderr,
                )
                if progress.aborted:
                    break
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop.set()
        reporter.join(timeout=5.0)
        progress.close()

    # Reconciliation, printed even on a clean run: it is the only check that
    # catches a SILENT undercount as well as an error-counted one.
    expected = len(done) + len(todo)
    cached = len(load_cache(a.cache))
    print(
        "\n  {:,} requested, {:,} cached, {:,} not fetched".format(
            expected, cached, expected - cached
        ),
        file=sys.stderr,
    )
    _report_drops(progress, len(todo), "fetch")
    if interrupted:
        sys.exit("\ninterrupted - {:,} fetched; re-run to resume.".format(progress.done))
    _report_abort(progress)


# ----------------------------------------------------------------- engaged
def cmd_engaged(a):
    """Addresses the user has actually written to. These are never auto-Trashed."""
    ids = list_ids("in:sent", a.sanitize)
    if a.limit:
        ids = ids[: a.limit]
    print("scanning {} sent messages".format(len(ids)), file=sys.stderr)
    print(_pacing_note(a), file=sys.stderr)
    addrs = set()
    seen = 0
    # Drops matter here too: a silently missing sent message weakens the
    # replied-to safeguard, and the empty-list guard below catches only total
    # failure, not partial.
    progress = FetchProgress(len(ids), drop_path=getattr(a, "dropped", "") or None)
    a.progress = progress
    stop, reporter = _with_reporter(progress)
    try:
        for rec in _scan(ids, a, progress, ENGAGED_HEADERS):
            if not rec:
                continue
            seen += 1
            for field in ("to", "cc", "bcc"):
                for m in ADDR.finditer(rec["headers"].get(field, "")):
                    addrs.add(m.group(0).lower())
    finally:
        stop.set()
        reporter.join(timeout=5.0)
        progress.close()
    _report_drops(progress, len(ids), "engaged")
    _report_abort(progress)

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
def _add_rate_args(sub, dropped_default=None):
    """The pacing knobs. Shared by every subcommand that hits the API in bulk."""
    sub.add_argument("--rate", type=float, default=0.0,
                     help="pin a fixed req/s; 0 (default) adapts. Throttles "
                          "still pause but never shrink a pinned rate")
    sub.add_argument("--max-rate", type=float, default=RATE_MAX,
                     help="ceiling on the adaptive search (default %(default)s)")
    sub.add_argument("--start-rate", type=float, default=RATE_START,
                     help="initial req/s (default %(default)s); advanced")
    sub.add_argument("--no-rate-limit", action="store_true",
                     help="disable pacing entirely - pre-limiter behaviour")
    if dropped_default is not None:
        sub.add_argument("--dropped", default=dropped_default,
                         help="JSONL of message IDs this run could not fetch "
                              "(default %(default)s); empty string disables")


def _make_limiter(a):
    """Resolve the process-wide limiter from the parsed arguments."""
    if not hasattr(a, "max_rate") or getattr(a, "no_rate_limit", False):
        return None
    pinned = a.rate and a.rate > 0
    return RateLimiter(
        rate=(a.rate if pinned else a.start_rate),
        burst=RATE_BURST,
        min_rate=RATE_MIN,
        max_rate=a.max_rate,
        adaptive=not pinned,
    )


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
    # 12, not 8: concurrency now has one job, covering latency
    # (workers ~= rate * 0.35s), so 8 workers would cap throughput at about
    # 23 req/s no matter what the limiter permits. 16 remains the documented
    # maximum - the API drops messages above it, which is a concurrency
    # effect the limiter does not repeal.
    f.add_argument("--concurrency", type=int, default=12)
    f.add_argument("--limit", type=int, default=0)
    _add_rate_args(f, FETCH_DROPPED)
    f.set_defaults(func=cmd_fetch)

    e = sub.add_parser("engaged", help="build replied-to address list")
    e.add_argument("--out", default="engaged.txt")
    e.add_argument("--concurrency", type=int, default=8)
    e.add_argument("--limit", type=int, default=0)
    _add_rate_args(e, ENGAGED_DROPPED)
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
    _add_rate_args(t)
    t.set_defaults(func=cmd_trash)

    u = sub.add_parser("untrash", help="restore messages from a manifest")
    u.add_argument("--manifest", default="trashed-manifest.jsonl")
    u.add_argument("--concurrency", type=int, default=8)
    u.add_argument("--execute", action="store_true")
    _add_rate_args(u)
    u.set_defaults(func=cmd_untrash)

    a = p.parse_args()
    global LIMITER
    LIMITER = _make_limiter(a)
    a.func(a)


if __name__ == "__main__":
    main()
