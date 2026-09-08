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
  rank      Score senders and emit the ranked index or a review file
  doctor    Check auth, scope and prerequisites before a long run
  status    Report on a scan running in another terminal
  trash     Trash messages from an explicitly approved sender list
  untrash   Restore from a manifest
  ui        Local web UI: preflight and live scan progress (read-only)

The only mutating API calls in this file are messages.trash and
messages.untrash. Permanent deletion is absent, not merely avoided: it needs
the https://mail.google.com/ scope, and this tool authenticates with
gmail.modify, under which Google itself refuses it.
"""
import argparse
import collections
import datetime
import hmac
import json
import os
import re
import random
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


RATE_DEFAULT = 8.0       # req/s the scan PINS unless --adaptive is passed.
                         # Measured against a real mailbox on 2026-09-08: pinned
                         # here it held 5.17 msg/s where the adaptive controller
                         # collapsed to the 1.0 floor and managed 3.66. Not a
                         # tuned optimum, just the fastest thing known to be
                         # stable. See docs/PLAN-RATE-LIMITER.md.
RATE_START = 8.0         # req/s at launch of an --adaptive search
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
# The engaged scan's resume checkpoint. .jsonl for the same reason the
# drop files are: it holds real recipient addresses and .gitignore carries
# a bare *.jsonl rule.
ENGAGED_CACHE = "engaged-cache.jsonl"


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


def _progress_reporter(progress, limiter, stop, interval=2.0, plain_every=10.0,
                       status=None):
    """Print the live line from its own thread, and publish it to disk.

    It has to be a thread rather than a print inside the ex.map consumer:
    printing from the consumer freezes during a global pause, which is the
    exact pathology this phase fixes. A stall should read as "backoff 12s",
    not as a frozen counter.

    The status file rides on this same tick rather than getting a thread of
    its own: it is the identical snapshot, and a second timer would be a
    second thing to stop on Ctrl-C.
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
        if status is not None:
            status.write(progress, limiter)
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


# The scan is an hour long and dies with the shell that started it. Without a
# file on disk, "how far along is the run in that other window?" has no
# answer, and cmd_ui can only report on scans it started itself.
FETCH_STATUS = "fetch-status.json"
ENGAGED_STATUS = "engaged-status.json"
STATUS_STALE_AFTER = 15.0  # ~7 missed reporter ticks


class StatusWriter:
    """Publish the live counters to a file for a reader in another process.

    Liveness is judged by the file's mtime and NEVER by probing the pid.
    os.kill(pid, 0) is the usual idiom and it is a trap here: on POSIX it
    tests for existence, but on Windows os.kill ignores the signal for
    anything but CTRL_C_EVENT/CTRL_BREAK_EVENT and calls TerminateProcess -
    so the same line that asks "is the scan alive?" would kill it. The pid is
    recorded for a human to act on, not for this module to signal.
    """

    def __init__(self, path, command, query="", cache=""):
        self.path = path or None
        self.command = command
        self.query = query
        self.cache = cache
        self.pid = os.getpid()
        # Wall clock, not monotonic: a monotonic value means nothing to
        # another process, and this file exists to be read by one.
        self.started = time.time()
        self._lock = threading.Lock()

    def write(self, progress, limiter, state="running"):
        if not self.path:
            return
        p = progress.snapshot()
        st = limiter.stats() if limiter is not None else None
        payload = {
            "command": self.command,
            "pid": self.pid,
            "state": state,
            # The query can name a sender, so this file is mailbox data and
            # is gitignored like the rest. It carries no addresses of its own.
            "query": self.query,
            "cache": self.cache,
            "started": self.started,
            "updated": time.time(),
            "total": p["total"],
            "done": p["done"],
            "dropped": p["dropped"],
            "elapsed": p["elapsed"],
            "rate": p["rate"],
            "avg_rate": p["avg_rate"],
            "eta": p["eta"],
            "aborted": p["aborted"],
            "limiter": None if st is None else {
                "rate": st["rate"], "state": st["state"],
                "throttles": st["throttles"],
            },
        }
        tmp = self.path + ".tmp"
        try:
            with self._lock:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                # Atomic on POSIX and on Windows: a reader polling this file
                # must never catch a half-written document.
                os.replace(tmp, self.path)
        except OSError:
            # Telemetry. A scan must never die because its status file could
            # not be written.
            pass


def read_status(path):
    """Return the status dict with staleness derived, or None."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    try:
        d["age"] = max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        d["age"] = 0.0
    # A killed scan leaves state "running" behind forever. The mtime is what
    # distinguishes "still going" from "the process is gone".
    d["stale"] = bool(d.get("state") == "running"
                      and d["age"] > STATUS_STALE_AFTER)
    d["path"] = path
    return d


def _fmt_ago(seconds):
    return "just now" if seconds < 1.5 else _fmt_eta(seconds) + " ago"


def _status_lines(d):
    """Render one status file as two lines."""
    state = "STALE" if d["stale"] else str(d.get("state", "?"))
    total, done = d.get("total") or 0, d.get("done") or 0
    pct = (100.0 * done / total) if total else 100.0
    # A stale file's last rate describes a process that no longer exists.
    # Printing it reads as live throughput, so it is suppressed.
    rate = 0.0 if d["stale"] else (d.get("rate") or 0.0)
    eta = 0.0 if d["stale"] else (d.get("eta") or 0)
    head = "{:<9}{:<9}{:>9}/{:<9}{:5.1f}%  {:5.1f} msg/s  eta {:<7} drops {}".format(
        d.get("command", "?"), state, "{:,}".format(done), "{:,}".format(total),
        pct, rate, _fmt_eta(eta), d.get("dropped") or 0,
    )
    lim = d.get("limiter")
    bits = []
    if lim:
        bits.append("limit {:.1f}/s {}".format(lim["rate"], lim["state"]))
    if d.get("query"):
        bits.append("query {}".format(d["query"]))
    bits.append("updated " + _fmt_ago(d["age"]))
    bits.append("pid {}".format(d.get("pid", "?")))
    tail = "         " + " · ".join(bits)
    lines = [head, tail]
    if d["stale"]:
        lines.append(
            "         the process is gone. Re-run {} to resume; the cache "
            "diffs IDs, so\n         nothing already fetched is "
            "re-requested.".format(d.get("command", "fetch"))
        )
    elif d.get("aborted"):
        lines.append("         aborted: {}".format(d["aborted"]))
    return lines


def cmd_status(a):
    """Report on a scan running in another terminal, or the last one to run."""
    paths = a.files or [FETCH_STATUS, ENGAGED_STATUS]
    found = [d for d in (read_status(p) for p in paths) if d]
    if a.json:
        print(json.dumps(found, indent=2))
        return
    if not found:
        print(
            "no scan status found.\n"
            "  Looked in: {}\n"
            "  A status file appears once 'fetch' or 'engaged' starts, and\n"
            "  survives the run, so this also reports the last completed "
            "scan.".format(", ".join(paths))
        )
        return
    for d in found:
        for line in _status_lines(d):
            print(line)


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


def _with_reporter(progress, status=None):
    """Start the reporter thread; returns (stop_event, thread)."""
    stop = threading.Event()
    t = threading.Thread(
        target=_progress_reporter, args=(progress, LIMITER, stop),
        kwargs={"status": status},
    )
    t.daemon = True
    t.start()
    return stop, t


def _final_state(progress, interrupted):
    """The terminal state a status reader should see once a scan stops."""
    if interrupted:
        return "interrupted"
    if progress.aborted:
        return "aborted"
    return "done"


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
    status = StatusWriter(getattr(a, "status", "") or None, "fetch",
                          query=a.query, cache=a.cache)

    interrupted = False
    stop, reporter = _with_reporter(progress, status)
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
        # The terminal state, so a reader after the fact sees an outcome
        # rather than a run that looks stalled forever.
        status.write(progress, LIMITER, _final_state(progress, interrupted))

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
def load_engaged_cache(path):
    """Return (scanned_ids, addrs) from a partial engaged scan.

    Same shape and same reason as the fetch cache: a 20-minute scan that dies
    at minute 18 should cost minutes to finish, not start over. Two real runs
    were lost to this before it existed.

    Only the message ID and the addresses extracted from it are stored, never
    the headers - it is the smallest thing that makes a re-run cheap. It still
    holds real recipient addresses, so it is .jsonl and gitignored like the
    rest of the mailbox data.
    """
    ids, addrs = set(), set()
    for rec in load_cache(path):
        mid = rec.get("id")
        if not mid:
            continue
        ids.add(mid)
        for addr in rec.get("addrs") or []:
            addrs.add(addr)
    return ids, addrs


def cmd_engaged(a):
    """Addresses the user has actually written to. These are never auto-Trashed."""
    ids = list_ids("in:sent", a.sanitize)
    if a.limit:
        ids = ids[: a.limit]

    cache_path = getattr(a, "cache", "") or None
    cached_ids, addrs = load_engaged_cache(cache_path)
    todo = [i for i in ids if i not in cached_ids]
    print(
        "{} sent messages, {} already scanned, {} to scan".format(
            len(ids), len(ids) - len(todo), len(todo)
        ),
        file=sys.stderr,
    )
    print(_pacing_note(a), file=sys.stderr)

    seen = len(cached_ids)
    # Drops matter here too: a silently missing sent message weakens the
    # replied-to safeguard, and the empty-list guard below catches only total
    # failure, not partial. A dropped ID is never checkpointed, so re-running
    # retries it.
    progress = FetchProgress(len(todo), drop_path=getattr(a, "dropped", "") or None)
    a.progress = progress
    status = StatusWriter(getattr(a, "status", "") or None, "engaged",
                          query="in:sent", cache=cache_path or a.out)
    stop, reporter = _with_reporter(progress, status)
    interrupted = False
    out = open(cache_path, "a", encoding="utf-8") if cache_path else None
    try:
        for rec in _scan(todo, a, progress, ENGAGED_HEADERS):
            if not rec:
                continue
            seen += 1
            found = set()
            for field in ("to", "cc", "bcc"):
                for m in ADDR.finditer(rec["headers"].get(field, "")):
                    found.add(m.group(0).lower())
            addrs |= found
            if out is not None:
                out.write(
                    json.dumps({"id": rec["id"], "addrs": sorted(found)},
                               ensure_ascii=False) + "\n"
                )
                # Per record, like the drop file: an interrupted run is
                # exactly when this file matters, and the write is trivial
                # next to the network call that produced it.
                out.flush()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop.set()
        reporter.join(timeout=5.0)
        progress.close()
        if out is not None:
            out.close()
        status.write(progress, LIMITER, _final_state(progress, interrupted))
    _report_drops(progress, len(todo), "engaged")

    # Neither an interrupted nor an aborted run writes engaged.txt, and that
    # is deliberate. require_engaged() only checks that the file EXISTS, so a
    # partial safeguard list is indistinguishable from a complete one - it
    # would pass the guard while quietly covering a fraction of the people you
    # write to. The checkpoint is what survives; the artifact is not.
    if interrupted:
        sys.exit(
            "\ninterrupted - {:,} of {:,} sent messages scanned and saved to "
            "{}.\nRe-run to resume; only the remainder is fetched.\n"
            "{} was NOT written: a partial safeguard list would look exactly "
            "like a\ncomplete one to every later step.".format(
                progress.done, len(todo), cache_path or "(no cache)", a.out)
        )
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


def load_engaged(path):
    """The replied-to safeguard list, or an empty set."""
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {l.strip().lower() for l in f if l.strip()}


def require_engaged(path, allow_missing, action):
    """Refuse to build or act on a trash list with the replied-to safeguard absent.

    A warning is not enough at these two points. `cmd_engaged` already
    refuses to write an empty list for exactly this reason: a silently inert
    safeguard is more dangerous than a loud failure, because the output looks
    correct either way. Without this list, `rank --review` marks people you
    correspond with as trashable, and `cmd_trash`'s SAFEGUARD OVERRIDE block
    under-reports - it can still see protected domains and stars, so it prints
    a confident, incomplete answer.

    A file that exists but is empty is a real answer: a mailbox with no sent
    mail. Only a MISSING file refuses, because only that means the step was
    never run.
    """
    if allow_missing or (path and os.path.exists(path)):
        return
    sys.exit(
        "no engaged-sender list at {}, so the 'you have written to this "
        "sender'\nsafeguard is INACTIVE. Refusing to {}.\n\n"
        "Without it, senders you actually correspond with are "
        "indistinguishable from\nstrangers: they get marked trashable, and "
        "the safeguard-override block will\nnot list them.\n\n"
        "    python gmail_audit.py engaged\n\n"
        "It is read-only and takes a few minutes. If you genuinely have no "
        "sent mail,\npass --allow-missing-engaged.".format(path, action)
    )


# How the IMPORTANT label is allowed to safeguard a sender. Gmail applies it
# automatically and often; STARRED is rare and deliberate. Under `any` - the
# original behaviour, kept as an option - a sender is immune once ONE of their
# messages was ever flagged, so for a sender with 100 messages the guard is
# essentially certain to fire no matter how well calibrated the label is. That
# is not a safeguard, it is a measurement of group size. On the first real
# mailbox this shipped against (5,192 senders, 34,953 messages) it immunised
# 100% of senders with 100+ messages and left 0.5% of the inbox trashable.
#
# `majority` keeps the signal where it means something and drops it where it is
# noise. STARRED is unaffected in every mode.
IMPORTANT_GUARD_MODES = ("off", "majority", "any")
IMPORTANT_GUARD_DEFAULT = "majority"


def important_guards(group, mode=IMPORTANT_GUARD_DEFAULT):
    """Does the IMPORTANT label safeguard this sender under `mode`?"""
    n = sum(1 for m in group if "IMPORTANT" in m["labelIds"])
    if not n or mode == "off":
        return False
    if mode == "any":
        return True
    return n * 2 > len(group)  # strict majority


def sender_guard(sender, group, engaged, important=IMPORTANT_GUARD_DEFAULT):
    """The false-positive safeguard for one sender, or None.

    Shared by cmd_rank and cmd_trash on purpose. cmd_trash recomputes it from
    the cache rather than trusting the [!] flag in a review file, so deleting
    that flag by hand removes the marker but not the warning.

    STARRED and IMPORTANT report separately. They are not the same evidence -
    one is a decision you made, the other is a guess Gmail made - and with
    `--important-guard` in play you cannot tell whether changing the mode moves
    a sender unless the row says which of the two held it.
    """
    if sender in engaged:
        return "replied-to"
    if any(p in sender for p in PROTECTED):
        return "protected-domain"
    if any("STARRED" in m["labelIds"] for m in group):
        return "starred"
    if important_guards(group, important):
        return "important"
    return None


def group_by_sender(msgs):
    by_sender = collections.defaultdict(list)
    for m in msgs:
        s = addr_of(m["headers"].get("from", ""))
        if s:
            by_sender[s].append(m)
    for group in by_sender.values():
        group.sort(key=lambda m: int(m.get("internalDate") or 0))
    return by_sender


def rank_rows(msgs, engaged=(), important=IMPORTANT_GUARD_DEFAULT):
    """Score every sender and return the ranked rows.

    Returning rows rather than printing them is what lets the table, the JSON
    and the review file be three renderings of ONE ranking rather than three
    rankings that have to be kept in agreement.
    """
    engaged = set(engaged)
    rows = []
    for sender, group in group_by_sender(msgs).items():
        score, signals = score_sender(sender, group)
        # False-positive safeguards apply AT THE TRASH BOUNDARY and nowhere
        # else. A guard exists to stop a Trash recommendation; a sender scoring
        # below 6 was never going to get one, so guarding them changes no
        # outcome and only lengthens the list a human has to read. Checking the
        # guard first - as this did - moved every guarded Keep into Review,
        # which is a promotion, not a demotion. On the first real mailbox that
        # was 1,303 senders scoring under 3, or 38% of the review pile, none of
        # them at any risk. They still carry [!] and --preselect-score still
        # refuses to mark them; they are simply not called out for review.
        guard = sender_guard(sender, group, engaged, important)
        if score >= 6:
            rec = "Review" if guard else "Trash"
        elif score >= 3:
            rec = "Review"
        else:
            rec = "Keep"
        rows.append({
            "sender": sender,
            "count": len(group),
            "score": score,
            "signals": signals,
            "rec": rec,
            "guard": guard,
            # Carried whether or not it guarded anything, so a row still shows
            # what --important-guard would have to work with. Dropping a
            # safeguard silently is how you get a surprise at trash time.
            "important": sum(1 for m in group if "IMPORTANT" in m["labelIds"]),
        })
    rows.sort(key=lambda r: (-r["score"], -r["count"]))
    return rows


def row_notes(row, with_guard=True):
    """The guard, the IMPORTANT coverage and the signals, as a human reads them.

    One helper so the table and the review file cannot drift. The coverage is
    shown even when it guarded nothing: that number is what tells you whether
    --important-guard any would move this row, and it is the number the
    majority rule is applied to. `with_guard=False` is for the table, which
    already prints the guard in its own column.
    """
    important = row.get("important", 0)
    tally = ["important:{}/{}".format(important, row["count"])] if important else []
    guard = [row["guard"]] if with_guard and row["guard"] else []
    if row["guard"] == "important":
        guard = []  # the tally says it better, and says it once
    return guard + tally + row["signals"]


def cmd_rank(a):
    msgs = load_cache(a.cache)
    if not msgs:
        sys.exit("no cached headers at {} - run 'fetch' first".format(a.cache))

    engaged = load_engaged(a.engaged)
    # Before the warning below, not after: on the refusal path the warning is
    # just noise in front of a longer message that says the same thing.
    if getattr(a, "review", None):
        require_engaged(a.engaged, getattr(a, "allow_missing_engaged", False),
                        "write a review file")
    if not engaged:
        print(
            "WARNING: no engaged-sender list ({}). The 'you have corresponded\n"
            "with this sender' safeguard is INACTIVE - senders you actually\n"
            "reply to may be scored as Trash. Run 'engaged' first.\n".format(
                a.engaged
            ),
            file=sys.stderr,
        )

    rows = rank_rows(msgs, engaged,
                     important=getattr(a, "important_guard",
                                       IMPORTANT_GUARD_DEFAULT))

    if getattr(a, "review", None):
        # Guarded above: the table is informational and keeps the warning,
        # but this file is what becomes a trash list.
        summary = write_review(
            a.review, rows,
            preselect_score=getattr(a, "preselect_score", 0),
            important=getattr(a, "important_guard", IMPORTANT_GUARD_DEFAULT),
            min_score=getattr(a, "min_score", 0))
        _report_review(a.review, summary)
        return

    if a.json:
        print(json.dumps(rows, indent=2))
        return

    print("{:<44}{:>6}{:>7}  {:<26}{}".format("sender", "n", "score", "recommendation", "signals"))
    print("-" * 128)
    for r in rows:
        tag = r["rec"] + ("(" + r["guard"] + ")" if r["guard"] else "")
        print(
            "{:<44}{:>6}{:>7}  {:<26}{}".format(
                r["sender"][:43], r["count"], r["score"], tag,
                ", ".join(row_notes(r, with_guard=False)),
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


# ------------------------------------------------------------------ review
# The ranked index and the approval list used to be two different artifacts,
# so a human retyped addresses from one into the other. That is transcription
# work, and a typo in it produces a no-op rather than an error. The review
# file is both: it is what `rank` emits and what `trash` reads, so a decision
# costs one character and never an address.
#
# It does NOT relax "approval is a list, not a threshold". Every row is
# written unmarked. --preselect-score marks rows for you, but it is a flag you
# have to type, and it will not mark a safeguarded sender under any
# circumstances. The file you save is still the decision.
REVIEW_FILE = "review.txt"
REVIEW_MARKS = {"t": True, ".": False}
REVIEW_GUARD_FLAG = "[!]"

REVIEW_HEADER = """\
# gmail-audit review
# {senders} senders, {messages} messages, generated {when}
#
# The mark column is the decision. 't' trashes every message from that
# sender; '.' leaves it alone. Nothing else is accepted - an unknown mark is
# an error, not a silently skipped line.
#
#     python gmail_audit.py trash --review {path}            # dry run
#     python gmail_audit.py trash --review {path} --execute
#
# {flag} is a safeguarded sender: you have written to them, they are on a
# protected domain, you starred them, or Gmail marks them important.
# --preselect-score never marks these, and trashing one takes a keystroke you
# typed on that row yourself.
#
# IMPORTANT guard: {important}. Gmail applies that label automatically, so
# `any` immunises almost every high-volume sender; `majority` is the default
# and `off` ignores it. STARRED always guards. Re-run rank with a different
# --important-guard to see the difference - your marks carry over.
#
{filter}# Re-running rank keeps the marks already in this file, so reviewing in
# several sittings is safe, and so is fetching more mail part way through.
#
# mark flag {sender:<{w}}{n:>6}{score:>7}  signals
"""

# Printed only for a filtered file. A truncated review file that does not say
# it is truncated is indistinguishable from a complete one, and the difference
# is thousands of senders.
REVIEW_FILTER_NOTE = """\
# FILTERED to senders scoring {min_score} or more, plus every sender already
# marked in this file. Not shown: {hidden}. Nothing was lost and nothing was
# decided for you - re-run without --min-score to see them all, and the marks
# below come with you.
#
"""


def _review_line(row, mark, width=44):
    # The sender is NEVER truncated. This column is parsed, not just read: a
    # clipped address either fails validation and makes the whole file
    # unreadable, or - worse - still looks like an address and silently names
    # a sender that does not exist. Real addresses run well past 43 characters;
    # the fixture's example.com ones do not, which is why this shipped.
    return "{}     {:<6}{:<{w}}{:>6}{:>7}  {}".format(
        mark,
        REVIEW_GUARD_FLAG if row["guard"] else "",
        row["sender"],
        row["count"],
        row["score"],
        ", ".join(row_notes(row)),
        w=width,
    ).rstrip()  # a Keep row has no signals; no trailing whitespace


def parse_review(path, strict=True):
    """Read a review file. Returns (marks, errors).

    marks maps sender -> True (trash) or False (leave alone). Parsing splits
    on whitespace, so re-aligning or re-ordering the file by hand is fine and
    only the mark and the address carry meaning.
    """
    marks, errors, seen = {}, [], {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(enumerate(f, 1))
    except OSError as e:
        return {}, ["cannot read {}: {}".format(path, e)]
    for n, raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        mark = parts[0]
        rest = parts[1:]
        if rest and rest[0] == REVIEW_GUARD_FLAG:
            rest = rest[1:]
        if mark not in REVIEW_MARKS:
            errors.append(
                "line {}: mark {!r} is not one of {}".format(
                    n, mark, " ".join(sorted(REVIEW_MARKS))))
            continue
        if not rest:
            errors.append("line {}: no sender address".format(n))
            continue
        sender = rest[0].lower()
        # A mangled address must not become a silent no-op - that is exactly
        # the failure mode this file exists to remove.
        if addr_of(sender) != sender:
            errors.append("line {}: {!r} is not an address".format(n, rest[0]))
            continue
        if sender in seen:
            errors.append(
                "line {}: {} already appears on line {}".format(
                    n, sender, seen[sender]))
            continue
        seen[sender] = n
        marks[sender] = REVIEW_MARKS[mark]
    return marks, (errors if strict else [])


def write_review(path, rows, preselect_score=0,
                 important=IMPORTANT_GUARD_DEFAULT, min_score=0):
    """Write the review file, carrying forward any marks already in it.

    Merging is the default and there is no overwrite flag: silently discarding
    a half-finished review is a worse failure than any it would prevent. A
    fresh file is one `rm` away.

    `min_score` hides rows below it, and hides ONLY undecided ones: a sender
    you have already marked is written whatever they score. That is what makes
    the filter a view rather than a rewrite. Without that rule, narrowing the
    file would silently discard decisions already made, and the file is the
    only record of them - the same failure the no-overwrite rule above exists
    to prevent, arriving through a different door.
    """
    prior, _ = parse_review(path, strict=False) if os.path.exists(path) else ({}, [])
    carried = kept_marked = preselected = 0
    # Captured before the filter runs: "dropped" must mean gone from the cache,
    # never merely hidden by --min-score.
    in_cache = {r["sender"] for r in rows}
    # Filter first, so preselect can only ever mark a row that is visible.
    hidden = 0
    if min_score:
        visible = []
        for row in rows:
            if row["score"] < min_score and not prior.get(row["sender"]):
                hidden += 1
            else:
                visible.append(row)
        rows = visible
    # Sized to the widest sender present rather than a constant, so the columns
    # still line up without ever clipping an address.
    width = max([44] + [len(r["sender"]) for r in rows]) + 2
    body = []
    for row in rows:
        sender = row["sender"]
        if sender in prior:
            carried += 1
            marked = prior[sender]
            if marked:
                kept_marked += 1
        elif preselect_score and not row["guard"] and row["score"] >= preselect_score:
            marked = True
            preselected += 1
        else:
            marked = False
        body.append(_review_line(row, "t" if marked else ".", width))

    header = REVIEW_HEADER.format(
        senders=len(rows),
        messages=sum(r["count"] for r in rows),
        when=datetime.datetime.now().isoformat(timespec="minutes"),
        path=path,
        flag=REVIEW_GUARD_FLAG,
        sender="sender", n="n", score="score", w=width,
        important=important,
        filter=(REVIEW_FILTER_NOTE.format(min_score=min_score, hidden=hidden)
                if min_score else ""),
    )
    # Via a temp file: this reads and rewrites the same path, so a crash part
    # way through would otherwise take the decisions with it.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(body) + "\n")
    os.replace(tmp, path)
    return {
        "senders": len(rows),
        "carried": carried,
        "kept_marked": kept_marked,
        "preselected": preselected,
        # Two different things, and conflating them would be a lie in the
        # scarier direction: "dropped" means gone from the cache, "hidden"
        # means one flag away from coming back.
        "dropped": len(set(prior) - in_cache),
        "hidden": hidden,
        "marked": kept_marked + preselected,
    }


def _plural(n, word):
    return "{} {}{}".format(n, word, "" if n == 1 else "s")


def _report_review(path, summary):
    print("wrote {} ({} senders)".format(path, summary["senders"]))
    if summary["carried"]:
        print("  carried forward {} marks from the existing file ({} still "
              "marked for trash)".format(summary["carried"],
                                         summary["kept_marked"]))
    if summary["dropped"]:
        print("  {} senders in the old file are no longer in the cache and "
              "were dropped".format(summary["dropped"]))
    if summary.get("hidden"):
        print("  not shown: {} below the score filter. Every sender you had "
              "marked is.".format(
                  _plural(summary["hidden"], "sender")))
    if summary["preselected"]:
        print("  pre-marked {} senders for trash; safeguarded senders were "
              "NOT marked".format(summary["preselected"]))
    print()
    print("Edit the mark column, then:")
    print("    python gmail_audit.py trash --review {}".format(path))
    if not summary["marked"]:
        print("\nNothing is marked yet. That is deliberate: an unmarked file "
              "trashes nothing.")


def load_review_approved(path):
    """The approved set from a review file. Exits on any parse error."""
    if not os.path.exists(path):
        sys.exit(
            "review file not found: {}\n"
            "Create it with:  python gmail_audit.py rank --review {}".format(
                path, path))
    marks, errors = parse_review(path, strict=True)
    if errors:
        sys.exit(
            "{} is not readable as a review file:\n  {}\n"
            "Refusing to guess what was meant - fix the lines above and "
            "re-run.".format(path, "\n  ".join(errors[:10])))
    approved = {s for s, marked in marks.items() if marked}
    if not approved:
        sys.exit(
            "nothing is marked 't' in {} - nothing to do.\n"
            "Change the mark column on the senders you want gone.".format(path))
    return approved


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
    source = getattr(a, "review", None)
    if source:
        approved = load_review_approved(source)
    else:
        source = a.senders
        if not os.path.exists(source):
            sys.exit(
                "approved sender list not found: {}\n"
                "Create it from the ranked index - one address per line - or "
                "use the\nreview file, which needs no transcription:\n"
                "    python gmail_audit.py rank --review {}\n"
                "    python gmail_audit.py trash --review {}\n"
                "Either way this command will not act on a score "
                "threshold.".format(source, REVIEW_FILE, REVIEW_FILE)
            )
        with open(source, encoding="utf-8") as f:
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

    # Recomputed from the cache, never read off the review file: deleting the
    # [!] flag by hand removes the marker, not the warning. Safeguards still
    # do not override the human's list - they are surfaced, then obeyed.
    #
    # Checked here too, not only at rank time: this is the step that moves
    # mail, and a review file can reach it having been written on another
    # machine or before the list existed.
    require_engaged(getattr(a, "engaged", ""),
                    getattr(a, "allow_missing_engaged", False),
                    "trash anything")
    engaged = load_engaged(getattr(a, "engaged", ""))
    groups = group_by_sender(msgs)
    # Same --important-guard the ranking used, and for the same reason it is
    # recomputed at all: the review file records a decision, not a safeguard.
    # A run that ranked with `off` and trashes with the default simply gets
    # MORE warnings here, which is the direction to be wrong in.
    important = getattr(a, "important_guard", IMPORTANT_GUARD_DEFAULT)
    overrides = {}
    for s in approved:
        if s in groups:
            guard = sender_guard(s, groups[s], engaged, important)
            if guard:
                overrides[s] = guard

    by_sender = collections.Counter(t["sender"] for t in targets)
    print("Approved senders : {} (from {})".format(len(approved), source))
    print("Matching messages: {}".format(len(targets)))
    print()
    for s, n in by_sender.most_common():
        flag = " {}".format(REVIEW_GUARD_FLAG) if s in overrides else ""
        print("  {:<48}{:>6}{}".format(s[:47], n, flag))
    missing = approved - set(by_sender)
    if missing and getattr(a, "review", None):
        # The review file was GENERATED from this cache, so a marked sender
        # with nothing to match did not come from the ranking - it was typed.
        # A transposed domain is still a syntactically valid address, so the
        # parser cannot catch it; this is where it stops being silent. The
        # plain --senders path keeps the softer note below, because there the
        # list is hand-written by design.
        sys.exit(
            "\n{} marked sender(s) have no messages in {}:\n  {}\n"
            "This file is generated from that cache, so a marked sender that "
            "matches nothing\nwas typed by hand and is probably a typo - a "
            "transposed domain is still a valid\naddress. Fix the line, or "
            "re-run: python gmail_audit.py rank --review {}".format(
                len(missing), a.cache, "\n  ".join(sorted(missing)), source))
    if missing:
        print("\n  (no cached messages for: {})".format(", ".join(sorted(missing))))

    if overrides:
        print()
        print("SAFEGUARD OVERRIDE - {} approved sender(s) are protected:".format(
            len(overrides)))
        for s in sorted(overrides):
            print("  {:<48}{:>6}  {}".format(
                s[:47], by_sender.get(s, 0), overrides[s]))
        print("  You have written to these, they are on a protected domain, you")
        print("  starred them, or Gmail marks them important. Nothing pre-marks a")
        print("  safeguarded sender; each of these was approved by hand.")

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

    # DESIGN-UI.md: a safeguarded sender must never be swept along, it has to
    # be an individual, deliberate act. Marking the row was the first half;
    # this is the second, and it is one prompt for the whole set rather than
    # one per sender because the marking was already per sender.
    if overrides and not a.yes:
        resp = input(
            "\n{} safeguarded sender(s) are in this run. Type 'override' to "
            "include\nthem, anything else to stop: ".format(len(overrides))
        )
        if resp.strip().lower() != "override":
            sys.exit("stopped - nothing was modified.")

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
# ------------------------------------------------------------------- web ui
# Phase 2 of docs/DESIGN-UI.md: a localhost server that reports preflight
# state and live scan progress.
#
# There is deliberately NO mutating endpoint in this phase. The design orders
# the token check and the escaping discipline BEFORE any endpoint that can
# trash mail, so both land here, guarding a surface that cannot yet delete
# anything. Landing them alongside the deletion path would mean the first
# version of that path is the one being tested.
#
# A localhost server that will one day trash mail is a different risk profile
# from a CLI, so four properties hold at once:
#   1. loopback bind, asserted rather than defaulted (_ui_bind_address)
#   2. a per-launch token on EVERY request, page included
#   3. Host and Origin allowlists, which is what defeats DNS rebinding
#   4. no CORS headers, ever, so a foreign page cannot read a response

UI_HOST = "127.0.0.1"
UI_PORT = 8765
UI_LOOPBACK = ("127.0.0.1", "localhost", "::1")
UI_MAX_CONCURRENCY = 16  # see README: above this the API drops messages

# The failure modes of `gws gmail users getProfile`, ordered most specific
# first. The literal strings come from the troubleshooting index in
# docs/SETUP.md; the looser status-code alternatives are the fallback for a
# gws that words things differently. Word boundaries on the numbers for the
# same reason THROTTLE has them: message IDs are lowercase hex and a bare 403
# would match inside one.
#
# Distinguishing these two cases is the whole point of preflight. `gws auth
# status` reports the scopes that were REQUESTED, not the ones Google
# granted, so "authenticated but with no Gmail scope" looks identical to a
# healthy install until a real call is made - after the user has waited an
# hour for a scan that could never have worked.
UI_ERRORS = (
    ("no_gws", re.compile(
        r"unrecognized subcommand|command not found|no such file or directory|"
        r"is not recognized as|\bENOENT\b|WinError 2", re.I)),
    ("bad_params", re.compile(
        r"invalid --params json|key must be a string", re.I)),
    ("insufficient_scope", re.compile(
        r"insufficient authentication scopes|insufficientPermissions|"
        r"ACCESS_TOKEN_SCOPE_INSUFFICIENT|insufficient[_ ]scope", re.I)),
    ("unauthenticated", re.compile(
        r"no credentials provided|access denied\. no credentials|"
        r"invalid[_ ]grant|\bUNAUTHENTICATED\b|\b401\b|"
        r"token has been expired or revoked|gws auth login", re.I)),
    ("insufficient_scope", re.compile(
        r"\b403\b|PERMISSION_DENIED|forbidden", re.I)),
)

# The four preflight outcomes, named once. cmd_doctor renders these and so
# does the page; test_preflight_labels_do_not_drift asserts the JS copy in
# UI_HTML covers exactly the same set.
PREFLIGHT_LABELS = {
    "ok": "authenticated",
    "unauthenticated": "not authenticated",
    "insufficient_scope": "authenticated, but no Gmail scope",
    "no_gws": "gws not found",
    "bad_params": "the shell mangled the JSON argument",
    "error": "check failed",
}

UI_HINTS = {
    "no_gws": "gws is not on PATH, or this shell predates the install. Open a "
              "new terminal; on Windows set GWS_BIN to the real .exe. "
              "SETUP.md > Troubleshooting.",
    "bad_params": "The shell mangled the JSON argument - PowerShell strips "
                  "inner quotes. SETUP.md > Platform notes.",
    "unauthenticated": "No usable credentials. Run: gws auth login --scopes "
                       "https://www.googleapis.com/auth/gmail.modify,openid,"
                       "https://www.googleapis.com/auth/userinfo.email",
    "insufficient_scope": "Authenticated, but the token carries no Gmail "
                          "scope. `gws auth status` will not show this - it "
                          "reports requested scopes, not granted ones. "
                          "SETUP.md > Request had insufficient authentication "
                          "scopes.",
    "error": "Unexpected failure. The raw message is above; SETUP.md indexes "
             "troubleshooting by literal error text.",
}


def classify_gws_error(text):
    """Map a gws failure to a preflight status. Pure, so it is testable."""
    s = text or ""
    for status, pattern in UI_ERRORS:
        if pattern.search(s):
            return status
    return "error"


def preflight(sanitize=None):
    """Verify auth with a REAL API call, and say which way it failed.

    Retries are deliberately shallow: preflight answers a question, and a
    user staring at a blank panel should not wait out six backoffs to learn
    that they are not logged in.
    """
    params = json.dumps({"userId": "me"})
    try:
        out = gws(["gmail", "users", "getProfile", "--params", params],
                  sanitize, retries=1, throttle_retries=2)
    except OSError as e:
        # subprocess could not exec the binary at all.
        return {"ok": False, "status": "no_gws", "detail": str(e)[:400],
                "hint": UI_HINTS["no_gws"]}
    except Exception as e:
        detail = str(e)[:400]
        status = classify_gws_error(detail)
        return {"ok": False, "status": status, "detail": detail,
                "hint": UI_HINTS.get(status, UI_HINTS["error"])}
    try:
        prof = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "status": "error", "detail": out[:400],
                "hint": UI_HINTS["error"]}
    return {
        "ok": True,
        "status": "ok",
        "email": prof.get("emailAddress", ""),
        "messages_total": prof.get("messagesTotal"),
        "threads_total": prof.get("threadsTotal"),
        # Stored for phase 5: an incremental rescan starts from this.
        "history_id": prof.get("historyId"),
        "detail": "",
        "hint": "",
    }


def _doctor_row(name, value, ok):
    return "  {:<12}{:<44}{}".format(name, str(value)[:43], "ok" if ok else "FAIL")


def cmd_doctor(a):
    """Check the things that stop a first run, before an hour is spent.

    Everything here is one API call or less. The point is that a newcomer
    learns which of the four failures they have from a command that costs
    nothing, rather than from a scan that dies twenty minutes in.
    """
    print("gmail-audit doctor\n")
    rows, ok = [], True

    version = ".".join(str(n) for n in sys.version_info[:3])
    py_ok = sys.version_info >= (3, 8)
    rows.append(_doctor_row("python", version, py_ok))
    ok = ok and py_ok

    # _find_gws() falls back to the bare name, which resolves to nothing if
    # gws is not installed - so presence has to be checked, not assumed.
    import shutil
    gws_path = GWS if (os.path.isabs(GWS) and os.path.exists(GWS)) else shutil.which(GWS)
    rows.append(_doctor_row("gws", gws_path or "not on PATH", bool(gws_path)))

    detail = hint = ""
    if gws_path:
        pf = preflight(a.sanitize)
        label = PREFLIGHT_LABELS.get(pf["status"], pf["status"])
        rows.append(_doctor_row("auth", label, pf["ok"]))
        if pf["ok"]:
            rows.append(_doctor_row(
                "mailbox",
                "{:,} messages, {:,} threads".format(
                    int(pf["messages_total"] or 0), int(pf["threads_total"] or 0)),
                True))
            rows.append(_doctor_row("account", pf["email"], True))
        else:
            detail, hint = pf["detail"], pf["hint"]
        ok = ok and pf["ok"]
    else:
        ok = False
        hint = UI_HINTS["no_gws"]

    for row in rows:
        print(row)
    if detail:
        # Verbatim and unwrapped: this is the literal string SETUP.md is
        # indexed by, so it has to stay greppable and copy-pasteable.
        print("\n  " + detail.replace("\n", "\n  "))
    if hint:
        import textwrap
        # break_long_words/break_on_hyphens off: the unauthenticated hint is a
        # command with a long scope URL in it, and the whole point is that it
        # can be copied. Overflowing the width beats splitting the URL.
        print("\n" + textwrap.fill(
            hint, width=76, initial_indent="  ", subsequent_indent="  ",
            break_long_words=False, break_on_hyphens=False))

    if not ok:
        sys.exit(
            "\nNot ready. Fix the above, then re-run:\n"
            "    python gmail_audit.py doctor\n"
            "docs/SETUP.md indexes troubleshooting by the literal error text."
        )
    # The engagement scan comes first for a reason - see require_engaged.
    nxt = ("engaged" if not os.path.exists(a.engaged)
           else "fetch --query in:inbox")
    print("\nReady. Next:\n    python gmail_audit.py {}".format(nxt))


class ScanState:
    """What the UI knows about the background scan.

    Deliberately thin. The scan's own counters live in FetchProgress, which
    already returns a plain dict; this only tracks the lifecycle around it,
    because a scan spends its first minutes inside list_ids() with no
    counters to report yet.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.status = "idle"   # idle | running | done | failed | aborted
        self.error = None
        self.query = None
        self.started = None
        self.finished = None

    def begin(self, query):
        """Claim the scan slot. False if one is already running."""
        with self._lock:
            if self.status == "running":
                return False
            self.status = "running"
            self.error = None
            self.query = query
            self.started = time.time()
            self.finished = None
            return True

    def end(self, status, error=None):
        with self._lock:
            self.status = status
            self.error = error
            self.finished = time.time()

    def snapshot(self):
        with self._lock:
            return {
                "status": self.status,
                "error": self.error,
                "query": self.query,
                "started": self.started,
                "finished": self.finished,
            }


def _ui_scan_args(base, query, concurrency, limit):
    """Build the namespace cmd_fetch expects from the UI's request.

    The UI is a front-end over the same function, not a second scan path, so
    everything the CLI takes is carried through unchanged and only the three
    fields the page exposes are overridden.
    """
    return argparse.Namespace(
        sanitize=getattr(base, "sanitize", None),
        query=query,
        cache=getattr(base, "cache", "headers.jsonl"),
        batch=getattr(base, "batch", 1000),
        concurrency=concurrency,
        limit=limit,
        dropped=getattr(base, "dropped", FETCH_DROPPED),
        # "" not FETCH_STATUS: the ui parser always supplies a real default,
        # so a namespace without one means disabled rather than "guess a path
        # and write into whatever directory we happen to be in".
        status=getattr(base, "status", ""),
    )


def _ui_run_scan(state, ns):
    """cmd_fetch on a background thread, with its exits turned into state.

    cmd_fetch reports an aborted or interrupted run with sys.exit(), which in
    a thread is a silently swallowed SystemExit. The message it carries is
    exactly what the page needs to show, so catch it rather than lose it.
    """
    global PROGRESS
    PROGRESS = None
    try:
        cmd_fetch(ns)
    except SystemExit as e:
        msg = str(e.code) if e.code not in (None, 0) else None
        state.end("aborted" if msg else "done", msg)
    except Exception as e:  # noqa: BLE001 - a dead thread must not be silent
        state.end("failed", "{}: {}".format(type(e).__name__, e)[:400])
    else:
        state.end("done")


class _UIHandler(BaseHTTPRequestHandler):
    """Every request passes the same four guards before it routes anywhere."""

    server_version = "gmail-audit"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # the live progress line owns stderr

    def handle_one_request(self):
        # Per request, not per connection: one handler instance serves several.
        self._body_read = False
        BaseHTTPRequestHandler.handle_one_request(self)

    def _drain_request_body(self):
        """Consume any unread request body before replying.

        Windows resets a connection that is closed with bytes still unread in
        its receive buffer, so the client sees WinError 10053 instead of the
        response that was actually sent. Every rejection path here replies
        without looking at the body, which is exactly when this bites: a POST
        to /api/scan with a bad token or a foreign Origin carries a JSON body
        nobody reads. The 403 is written correctly and then destroyed by the
        close, and the page shows a network error in place of the reason.

        Bounded by the same 64 KiB `_read_json` accepts. Past that the body is
        left unread on purpose: resetting on a client sending megabytes we have
        already refused is the right outcome.
        """
        if self._body_read:
            return
        self._body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            return
        if n <= 0 or n > 64 * 1024:
            return
        while n > 0:
            chunk = self.rfile.read(min(n, 8192))
            if not chunk:
                break
            n -= len(chunk)

    # ------------------------------------------------------------- guards
    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {"127.0.0.1:{}".format(port), "localhost:{}".format(port),
                "[::1]:{}".format(port)}

    def _allowed_origins(self):
        return {"http://" + h for h in self._allowed_hosts()}

    def _guard(self):
        """Return an error string, or None to proceed."""
        # A rebinding attack reaches 127.0.0.1 while the browser still sends
        # the attacker's name in Host. An allowlist is what breaks that.
        if (self.headers.get("Host") or "").lower() not in self._allowed_hosts():
            return "bad Host"
        origin = self.headers.get("Origin")
        # Absent on same-origin navigation and on same-origin GETs; present
        # and foreign is the case that matters.
        if origin is not None and origin.lower() not in self._allowed_origins():
            return "bad Origin"
        if not self._token_ok():
            return "bad token"
        return None

    def _token_ok(self):
        want = self.server.ui_token
        got = self.headers.get("X-Audit-Token")
        if got is None:
            got = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query
            ).get("t", [""])[0]
        # Bytes, not str: compare_digest raises TypeError on a non-ASCII
        # str, and the query string is attacker-supplied - a 500 there would
        # be a denial of service on the page itself.
        return hmac.compare_digest(
            str(got).encode("utf-8", "replace"), str(want).encode("utf-8")
        )

    # ------------------------------------------------------------ replies
    def _send(self, code, body, ctype):
        # Before the response, not after: see _drain_request_body.
        self._drain_request_body()
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        # No CORS allow-origin header is emitted anywhere in this file, and
        # a test asserts the absence: without one a foreign page may fire a
        # request but can never read the answer.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # The page loads nothing and talks to nobody but this server, so a
        # future injected <script> would have no channel to send anything out.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    # ------------------------------------------------------------ routing
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        bad = self._guard()
        if bad:
            # The page gets prose it can act on; the SPA's fetch() gets JSON,
            # because a text/plain body would surface as a parse error and
            # read like the server had died rather than like a stale token.
            if path.startswith("/api/"):
                self._json(403, {"error": bad})
            else:
                self._send(403, UI_FORBIDDEN, "text/plain")
            return
        if path == "/":
            self._send(200, UI_HTML, "text/html")
        elif path == "/api/preflight":
            self._json(200, preflight(getattr(self.server.ui_args, "sanitize", None)))
        elif path == "/api/progress":
            self._json(200, self._progress())
        else:
            self._json(404, {"error": "no such endpoint"})

    def do_POST(self):
        bad = self._guard()
        if bad:
            self._json(403, {"error": bad})
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/scan":
            self._start_scan()
        else:
            self._json(404, {"error": "no such endpoint"})

    # Anything else - including the OPTIONS preflight a cross-origin fetch
    # would send - is refused rather than answered.
    def do_OPTIONS(self):
        self._send(405, "", "text/plain")

    def do_PUT(self):
        self._send(405, "", "text/plain")

    def do_DELETE(self):
        self._send(405, "", "text/plain")

    # ----------------------------------------------------------- handlers
    def _progress(self):
        scan = self.server.ui_scan.snapshot()
        progress = PROGRESS.snapshot() if PROGRESS is not None else None
        scan["source"] = "in-process"
        external_limiter = None
        # list_ids() runs before any counter exists, and on a large mailbox
        # that is minutes. Naming the phase is the difference between "still
        # enumerating" and "wedged".
        if scan["status"] == "running":
            scan["phase"] = "listing" if progress is None else "fetching"
        else:
            scan["phase"] = scan["status"]
            # A scan started from a terminal is the common case - the CLI is
            # the reference path. Reading its status file is what stops this
            # page from reporting "idle" over a running hour-long fetch.
            ext = read_status(getattr(self.server.ui_args, "status", ""))
            if ext:
                scan["source"] = "file"
                scan["query"] = ext.get("query")
                scan["phase"] = "stale" if ext["stale"] else ext.get("state")
                # This process's limiter is idle and says nothing about the
                # run being reported. Showing it would read as that run's pace.
                external_limiter = None if ext["stale"] else ext.get("limiter")
                progress = {
                    "total": ext.get("total") or 0,
                    "done": ext.get("done") or 0,
                    "dropped": ext.get("dropped") or 0,
                    "elapsed": ext.get("elapsed") or 0.0,
                    "rate": 0.0 if ext["stale"] else (ext.get("rate") or 0.0),
                    "avg_rate": ext.get("avg_rate") or 0.0,
                    "eta": 0.0 if ext["stale"] else (ext.get("eta") or 0.0),
                    "aborted": ext.get("aborted"),
                }
        limiter = LIMITER.stats() if LIMITER is not None else None
        if scan["source"] == "file":
            limiter = external_limiter
        return {"scan": scan, "progress": progress, "limiter": limiter}

    def _read_json(self):
        # This IS the body consumer, so it claims the read before doing it:
        # _send must not then try to drain bytes that are already gone.
        self._body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 64 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _start_scan(self):
        body = self._read_json()
        query = str(body.get("query") or "in:inbox").strip() or "in:inbox"
        try:
            # Explicit None checks, not `or`: a posted 0 is a wrong value the
            # user should be told about, not a falsy one that silently
            # becomes the default.
            raw_c, raw_l = body.get("concurrency"), body.get("limit")
            concurrency = 12 if raw_c is None else int(raw_c)
            limit = 0 if raw_l is None else int(raw_l)
        except (TypeError, ValueError):
            self._json(400, {"error": "concurrency and limit must be integers"})
            return
        if concurrency < 1 or concurrency > UI_MAX_CONCURRENCY:
            # Refused rather than clamped. Above 16 the API drops messages,
            # which undercounts senders and corrupts the ranking; silently
            # correcting the number would hide that the user asked for it.
            self._json(400, {"error": "concurrency must be 1-{} - above that "
                                      "the API drops messages and the ranking "
                                      "undercounts".format(UI_MAX_CONCURRENCY)})
            return
        if limit < 0:
            self._json(400, {"error": "limit must be >= 0"})
            return
        # A scan running in a terminal shares this mailbox's quota and this
        # cache. The button is disabled for it, but the guard belongs here:
        # the page is not the only thing that can post to this endpoint.
        ext = read_status(getattr(self.server.ui_args, "status", ""))
        if ext and ext.get("state") == "running" and not ext["stale"]:
            self._json(409, {"error": "a scan started elsewhere is still "
                                      "running (pid {})".format(
                                          ext.get("pid", "?"))})
            return
        state = self.server.ui_scan
        if not state.begin(query):
            self._json(409, {"error": "a scan is already running"})
            return
        ns = _ui_scan_args(self.server.ui_args, query, concurrency, limit)
        t = threading.Thread(target=_ui_run_scan, args=(state, ns))
        t.daemon = True
        t.start()
        self._json(202, {"started": True, "query": query})


def _ui_bind_address(host):
    """Refuse to bind anything but loopback.

    http.server binds 0.0.0.0 by default. Left alone that would put a scan
    trigger - and, from phase 4, mail deletion - on every interface of the
    machine. There is no flag to override this: absent capability beats
    remembered intent.
    """
    if host not in UI_LOOPBACK:
        raise ValueError(
            "refusing to bind {!r}: this server is loopback-only".format(host)
        )
    return host


def make_ui_server(port=UI_PORT, token=None, args=None, host=UI_HOST):
    httpd = ThreadingHTTPServer((_ui_bind_address(host), port), _UIHandler)
    httpd.daemon_threads = True
    httpd.ui_token = token or secrets.token_urlsafe(32)
    httpd.ui_args = args
    httpd.ui_scan = ScanState()
    return httpd


def cmd_ui(a):
    # Per launch, and never written to disk: the terminal that started the
    # server is the only place it exists besides the browser's address bar.
    token = secrets.token_urlsafe(32)
    try:
        httpd = make_ui_server(a.port, token, a)
    except OSError as e:
        sys.exit(
            "cannot listen on 127.0.0.1:{}: {}\n"
            "Something else is using that port. Pick another with --port, or "
            "--port 0\nto let the OS choose one.".format(a.port, e)
        )
    port = httpd.server_address[1]
    url = "http://{}:{}/?t={}".format(UI_HOST, port, token)
    print("gmail-audit ui on {}".format(url))
    print("  loopback only, token required on every request")
    print("  this phase can start a scan; it cannot trash anything")
    print("  Ctrl-C to stop")
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass  # a headless box is not a failure; the URL is printed above
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
        if LIMITER is not None:
            LIMITER.shutdown()  # workers drain instead of sleeping out a wait
        if PROGRESS is not None:
            PROGRESS.abort("interrupted")
    finally:
        httpd.server_close()


# --------------------------------------------------------------- the page
# One file, inlined CSS and JS, no network fetch of any kind - which is what
# lets the Content-Security-Policy above be 'none' for everything except this
# server.
#
# Nothing below writes markup - every dynamic value goes in as text, and
# test_ui_page_never_writes_markup enforces it by name. This is the same
# reasoning that keeps Subject out of score_sender(): header text is chosen by
# the sender, and a browser executes what a terminal merely printed. Phase 3
# renders Subject and From on this page while it holds a token; the discipline
# has to already be in place by then, not arrive with the feature that needs
# it.
UI_FORBIDDEN = (
    "403 - this server requires the per-launch token.\n\n"
    "Open the URL printed by `python gmail_audit.py ui`. The token is\n"
    "generated per launch and is not written to disk, so a stale bookmark\n"
    "will not work.\n"
)

UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>gmail-audit</title>
<style>
  :root {
    --bg: #fbfbfa; --fg: #1c1c1a; --dim: #6b6b66; --line: #e2e2dd;
    --card: #ffffff; --ok: #1a7f45; --warn: #a05a00; --bad: #b3261e;
    --accent: #2b5fd9;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16161a; --fg: #e8e8e4; --dim: #9a9a94; --line: #2e2e34;
      --card: #1e1e23; --ok: #4ec97e; --warn: #e0a13c; --bad: #ef6c60;
      --accent: #7aa2f7;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Helvetica, Arial, sans-serif;
  }
  main { max-width: 820px; margin: 0 auto; padding: 28px 20px 60px; }
  h1 { font-size: 19px; margin: 0 0 2px; letter-spacing: -0.01em; }
  h2 { font-size: 13px; margin: 0 0 14px; text-transform: uppercase;
       letter-spacing: 0.08em; color: var(--dim); font-weight: 600; }
  .sub { color: var(--dim); margin: 0 0 26px; font-size: 13px; }
  section {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 18px 20px; margin-bottom: 18px;
  }
  .row { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end; }
  label { display: block; font-size: 12px; color: var(--dim); margin-bottom: 4px; }
  input {
    font: inherit; padding: 6px 9px; border: 1px solid var(--line);
    border-radius: 6px; background: var(--bg); color: var(--fg);
  }
  input[type=number] { width: 84px; }
  #query { min-width: 240px; }
  button {
    font: inherit; font-weight: 550; padding: 7px 15px; border-radius: 6px;
    border: 1px solid var(--accent); background: var(--accent); color: #fff;
    cursor: pointer;
  }
  button.ghost { background: transparent; color: var(--fg);
                 border-color: var(--line); }
  button[disabled] { opacity: 0.45; cursor: default; }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
    gap: 14px 18px; margin-top: 4px;
  }
  .k { font-size: 11px; color: var(--dim); text-transform: uppercase;
       letter-spacing: 0.06em; }
  .v { font-size: 17px; font-variant-numeric: tabular-nums; margin-top: 1px; }
  .bar { height: 7px; background: var(--line); border-radius: 4px;
         overflow: hidden; margin: 16px 0 4px; }
  .bar > div { height: 100%; width: 0; background: var(--accent);
               transition: width 0.4s ease; }
  .note { color: var(--dim); font-size: 12px; margin-top: 10px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
          font-size: 12px; }
  .raw { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         font-size: 12px; white-space: pre-wrap; word-break: break-word; }
  .barline { display: flex; align-items: baseline; justify-content: flex-end;
             gap: 8px; margin: 6px 0 16px; font-variant-numeric: tabular-nums; }
  .status { font-weight: 600; }
  .ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); }
  .banner { border-left: 3px solid var(--line); padding: 8px 0 8px 12px;
            margin-top: 12px; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<main>
  <h1>gmail-audit</h1>
  <p class="sub">Headers only. Loopback only. This page cannot trash
    anything &mdash; it reads and it scans.</p>

  <section>
    <h2>1 &middot; Preflight</h2>
    <div class="row">
      <button id="pf-btn" class="ghost" type="button">Re-check</button>
      <span id="pf-status" class="status">checking&hellip;</span>
    </div>
    <div id="pf-ok" hidden>
      <div class="grid">
        <div><div class="k">account</div><div class="v" id="pf-email">&mdash;</div></div>
        <div><div class="k">messages</div><div class="v" id="pf-messages">&mdash;</div></div>
        <div><div class="k">threads</div><div class="v" id="pf-threads">&mdash;</div></div>
      </div>
    </div>
    <div id="pf-bad" class="banner" hidden>
      <div class="raw" id="pf-detail"></div>
      <div class="note" id="pf-hint"></div>
    </div>
    <p class="note">A real getProfile call, not <span class="mono">gws auth status</span>
      &mdash; which reports the scopes that were requested, not the ones
      Google granted.</p>
  </section>

  <section>
    <h2>2 &middot; Scan</h2>
    <div class="row">
      <div>
        <label for="query">query</label>
        <input id="query" type="text" value="in:inbox" spellcheck="false">
      </div>
      <div>
        <label for="concurrency">concurrency</label>
        <input id="concurrency" type="number" min="1" max="16" value="12">
      </div>
      <div>
        <label for="limit">limit (0 = all)</label>
        <input id="limit" type="number" min="0" value="0">
      </div>
      <button id="scan-btn" type="button">Start scan</button>
      <span id="scan-status" class="status">idle</span>
    </div>
    <p class="note" id="scan-source" hidden></p>
    <div class="bar"><div id="bar"></div></div>
    <div class="barline">
      <span class="k">limiter</span><span class="mono" id="p-limit">&mdash;</span>
    </div>
    <div class="grid">
      <div><div class="k">fetched</div><div class="v" id="p-done">&mdash;</div></div>
      <div><div class="k">rate</div><div class="v" id="p-rate">&mdash;</div></div>
      <div><div class="k">average</div><div class="v" id="p-avg">&mdash;</div></div>
      <div><div class="k">eta</div><div class="v" id="p-eta">&mdash;</div></div>
      <div><div class="k">dropped</div><div class="v" id="p-drops">&mdash;</div></div>
    </div>
    <div id="scan-error" class="banner raw" hidden></div>
    <p class="note">Resumable: re-running skips whatever is
      already in the cache. Above concurrency 16 the API drops messages, which
      undercounts senders and corrupts the ranking &mdash; so the server
      refuses it rather than quietly lowering it.</p>
  </section>

  <p class="note">Ranking and selection arrive in phase 3; until then use
    <span class="mono">python gmail_audit.py rank</span>.</p>
</main>
<script>
(function () {
  "use strict";
  var TOKEN = new URLSearchParams(window.location.search).get("t") || "";

  function $(id) { return document.getElementById(id); }
  // The only way text reaches this page, and deliberately the only one:
  // every value below ultimately comes from a mailbox, and a browser
  // executes what a terminal merely printed.
  function put(id, value) { $(id).textContent = value; }
  function show(id, on) { $(id).hidden = !on; }
  function cls(id, name) { $(id).className = "status " + (name || ""); }

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign(
      { "X-Audit-Token": TOKEN }, options.headers || {}
    );
    options.cache = "no-store";
    return fetch(path, options).then(function (r) {
      return r.json().then(function (body) {
        return { code: r.status, body: body };
      });
    });
  }

  function num(n) {
    return (n === null || n === undefined) ? "—" : n.toLocaleString();
  }

  function eta(seconds) {
    if (!seconds || seconds <= 0) { return "—"; }
    var s = Math.round(seconds);
    if (s >= 3600) {
      return Math.floor(s / 3600) + "h" +
             String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m";
    }
    if (s >= 60) {
      return Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") + "s";
    }
    return s + "s";
  }

  // ------------------------------------------------------------ preflight
  var PF_LABEL = {
    ok: "authenticated",
    unauthenticated: "not authenticated",
    insufficient_scope: "authenticated, but no Gmail scope",
    no_gws: "gws not found",
    bad_params: "the shell mangled the JSON argument",
    error: "check failed"
  };

  function preflight() {
    $("pf-btn").disabled = true;
    put("pf-status", "checking…");
    cls("pf-status", "");
    return api("/api/preflight").then(function (r) {
      var d = r.body || {};
      put("pf-status", PF_LABEL[d.status] || d.status || "unknown");
      cls("pf-status", d.ok ? "ok" : "bad");
      show("pf-ok", !!d.ok);
      show("pf-bad", !d.ok);
      if (d.ok) {
        put("pf-email", d.email || "—");
        put("pf-messages", num(d.messages_total));
        put("pf-threads", num(d.threads_total));
      } else {
        put("pf-detail", d.detail || "");
        put("pf-hint", d.hint || "");
      }
    }).catch(function (e) {
      put("pf-status", "could not reach the local server");
      cls("pf-status", "bad");
      show("pf-bad", true);
      put("pf-detail", String(e));
      put("pf-hint", "The server may have been stopped, or the token in this "
                     + "URL is stale. Restart with: python gmail_audit.py ui");
    }).then(function () { $("pf-btn").disabled = false; });
  }

  // ----------------------------------------------------------------- scan
  var PHASE_LABEL = {
    idle: "idle",
    listing: "enumerating message IDs…",
    fetching: "fetching headers",
    running: "running",
    done: "complete",
    aborted: "aborted",
    interrupted: "interrupted",
    stale: "stopped without finishing",
    failed: "failed"
  };
  var timer = null;

  function startScan() {
    $("scan-btn").disabled = true;
    show("scan-error", false);
    api("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: $("query").value,
        concurrency: Number($("concurrency").value),
        limit: Number($("limit").value)
      })
    }).then(function (r) {
      if (r.code !== 202) {
        show("scan-error", true);
        put("scan-error", (r.body && r.body.error) || ("HTTP " + r.code));
        $("scan-btn").disabled = false;
        return;
      }
      poll();
    }).catch(function (e) {
      show("scan-error", true);
      put("scan-error", String(e));
      $("scan-btn").disabled = false;
    });
  }

  function render(d) {
    var scan = d.scan || {};
    var p = d.progress;
    var lim = d.limiter;
    var running = scan.status === "running";

    var external = scan.source === "file";
    put("scan-status", PHASE_LABEL[scan.phase] || scan.phase || "idle");
    cls("scan-status", (running || scan.phase === "running") ? "warn"
        : (scan.phase === "done" ? "ok"
        : (scan.phase === "idle" ? "" : "bad")));
    // Disabled for a scan started elsewhere too: it shares this mailbox's
    // quota and this cache, and the server refuses it either way.
    $("scan-btn").disabled = running || scan.phase === "running";

    // The CLI is the reference path, so a scan is at least as likely to have
    // been started from a terminal as from this page.
    show("scan-source", external);
    if (external) {
      put("scan-source", scan.phase === "stale"
        ? "A scan started elsewhere stopped without finishing. Re-run it; the "
          + "cache resumes."
        : "Reading a scan started in another terminal"
          + (scan.query ? " (" + scan.query + ")" : "") + ".");
    }

    if (scan.error) {
      show("scan-error", true);
      put("scan-error", scan.error);
    }

    if (p) {
      var pct = p.total ? (100 * p.done / p.total) : 0;
      $("bar").style.width = Math.min(100, pct).toFixed(1) + "%";
      put("p-done", num(p.done) + " / " + num(p.total));
      put("p-rate", p.rate.toFixed(1) + " msg/s");
      put("p-avg", p.avg_rate.toFixed(1) + " msg/s");
      put("p-eta", eta(p.eta));
      // Never hidden behind a fold: a fast run that drops messages is a
      // failed run, not a partial success.
      put("p-drops", num(p.dropped));
      $("p-drops").className = "v" + (p.dropped ? " bad" : "");
    }
    put("p-limit", lim ? (lim.rate.toFixed(1) + "/s " + lim.state
                          + (lim.throttles ? "  thr " + lim.throttles : ""))
                       : "off");
    return running;
  }

  function poll() {
    if (timer) { clearTimeout(timer); timer = null; }
    api("/api/progress").then(function (r) {
      var d = r.body || {};
      var running = render(d);
      var live = running || (d.scan && d.scan.phase === "running");
      timer = setTimeout(poll, live ? 1000 : 4000);
    }).catch(function () {
      timer = setTimeout(poll, 4000);
    });
  }

  $("pf-btn").addEventListener("click", preflight);
  $("scan-btn").addEventListener("click", startScan);
  preflight();
  poll();
})();
</script>
</body>
</html>
"""


def _add_rate_args(sub, dropped_default=None, status_default=None):
    """The pacing knobs, plus the two per-run side files that ride with them."""
    # Pinned by default. The adaptive controller is measurably worse than a
    # fixed rate on a real mailbox, so it is opt-in until it is fixed rather
    # than the thing every first run gets. --rate 0 still means "adapt", which
    # is what it has always meant.
    #
    # Not an argparse mutually_exclusive_group, deliberately. That rejected the
    # pair with "argument --adaptive: not allowed with argument --rate", which
    # says what is refused and not what to type instead - and since --rate now
    # carries a default, "pin at 8 and also adapt" is a reasonable thing to
    # have believed you were asking for. _make_limiter refuses it with the
    # answer attached. Hence default=None: it is the only way to tell "--rate 8"
    # from the default, and the conflict is exactly about what was TYPED.
    sub.add_argument("--rate", type=float, default=None,
                     help="pin a fixed req/s (default {}). Throttles still "
                          "pause but never shrink a pinned rate. 0 means "
                          "adapt".format(RATE_DEFAULT))
    sub.add_argument("--adaptive", action="store_true",
                     help="search for the rate instead of pinning it. "
                          "KNOWN BROKEN on a per-minute quota: it collapses "
                          "to the floor and is slower. See "
                          "docs/PLAN-RATE-LIMITER.md")
    sub.add_argument("--max-rate", type=float, default=RATE_MAX,
                     help="ceiling on the --adaptive search (default %(default)s)")
    sub.add_argument("--start-rate", type=float, default=RATE_START,
                     help="initial req/s for --adaptive (default %(default)s)")
    sub.add_argument("--no-rate-limit", action="store_true",
                     help="disable pacing entirely - pre-limiter behaviour")
    if dropped_default is not None:
        sub.add_argument("--dropped", default=dropped_default,
                         help="JSONL of message IDs this run could not fetch "
                              "(default %(default)s); empty string disables")
    if status_default is not None:
        sub.add_argument("--status", default=status_default,
                         help="live progress published here for 'status' and "
                              "the UI to read (default %(default)s); empty "
                              "string disables")


def _make_limiter(a):
    """Resolve the process-wide limiter from the parsed arguments."""
    if not hasattr(a, "max_rate") or getattr(a, "no_rate_limit", False):
        return None
    # getattr, not attribute access: the UI and several tests build a namespace
    # by hand and predate these flags.
    rate = getattr(a, "rate", None)
    adaptive = getattr(a, "adaptive", False)
    if rate is not None and adaptive:
        # Columns computed, not hand-padded: the option strings carry the rate
        # the user typed, so their width is not known here.
        choices = [
            ("--rate {:g}".format(rate), "pin it there"),
            ("--adaptive --start-rate {:g}".format(rate),
             "begin the search there"),
            ("(nothing)",
             "pin at {:g}, the measured default".format(RATE_DEFAULT)),
        ]
        w = max(len(opt) for opt, _ in choices) + 4
        sys.exit(
            "--rate and --adaptive are mutually exclusive: one pins the rate, "
            "the other\nsearches for it.\n\n{}\n\n"
            "The search is the broken half - see docs/PLAN-RATE-LIMITER.md - "
            "so unless you\nare investigating it, drop --adaptive.".format(
                "\n".join("  {:<{w}}{}".format(o, t, w=w)
                          for o, t in choices)))
    if rate is None:
        # An --adaptive run wants the search, which is what rate 0 selects.
        rate = 0.0 if adaptive else RATE_DEFAULT
    pinned = rate > 0
    return RateLimiter(
        rate=(rate if pinned else a.start_rate),
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

    d = sub.add_parser("doctor", help="check auth and prerequisites before a run")
    d.add_argument("--engaged", default="engaged.txt",
                   help="checked only to suggest the next command")
    d.set_defaults(func=cmd_doctor)

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
    _add_rate_args(f, FETCH_DROPPED, FETCH_STATUS)
    f.set_defaults(func=cmd_fetch)

    e = sub.add_parser("engaged", help="build replied-to address list")
    e.add_argument("--out", default="engaged.txt")
    e.add_argument("--cache", default=ENGAGED_CACHE,
                   help="resume checkpoint (default %(default)s); a killed "
                        "scan resumes from here. Empty string disables it")
    e.add_argument("--concurrency", type=int, default=8)
    e.add_argument("--limit", type=int, default=0)
    _add_rate_args(e, ENGAGED_DROPPED, ENGAGED_STATUS)
    e.set_defaults(func=cmd_engaged)

    r = sub.add_parser("rank", help="score senders")
    r.add_argument("--cache", default="headers.jsonl")
    r.add_argument("--engaged", default="engaged.txt")
    r.add_argument("--json", action="store_true")
    r.add_argument("--review", nargs="?", const=REVIEW_FILE, default=None,
                   help="write a reviewable file (default %(const)s) instead "
                        "of the table; marks already in it are carried "
                        "forward")
    r.add_argument("--allow-missing-engaged", action="store_true",
                   help="write a review file with the replied-to safeguard "
                        "INACTIVE. Only for a mailbox with no sent mail")
    r.add_argument("--important-guard", choices=IMPORTANT_GUARD_MODES,
                     default=IMPORTANT_GUARD_DEFAULT, metavar="MODE",
                     help="how Gmail's IMPORTANT label safeguards a sender: "
                          "off | majority (default) | any. Gmail applies it "
                          "automatically, so 'any' immunises nearly every "
                          "high-volume sender. STARRED always guards.")
    r.add_argument("--min-score", type=int, default=0, metavar="N",
                   help="write only senders scoring >= N, plus every sender "
                        "already marked in the file. A view, not a rewrite: "
                        "nothing is decided or discarded, and re-running "
                        "without it brings the rest back with your marks "
                        "intact. --min-score 6 is the list the tool actually "
                        "has an opinion about")
    r.add_argument("--preselect-score", type=int, default=0, metavar="N",
                   help="pre-mark unguarded senders scoring >= N. Safeguarded "
                        "senders are never marked. Off by default, because "
                        "the friction being removed is partly protective")
    r.set_defaults(func=cmd_rank)

    st = sub.add_parser("status", help="report on a scan running elsewhere")
    st.add_argument("files", nargs="*",
                    help="status files to read (default: {} and {})".format(
                        FETCH_STATUS, ENGAGED_STATUS))
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    t = sub.add_parser("trash", help="trash messages from approved senders")
    t.add_argument("--senders", default="approved.txt",
                   help="approved sender addresses, one per line")
    t.add_argument("--review", default=None,
                   help="a review file from 'rank --review'; takes precedence "
                        "over --senders and needs no transcription")
    t.add_argument("--engaged", default="engaged.txt",
                   help="replied-to list, used to flag safeguard overrides "
                        "(default %(default)s)")
    t.add_argument("--allow-missing-engaged", action="store_true",
                   help="trash with the replied-to safeguard INACTIVE. Only "
                        "for a mailbox with no sent mail")
    t.add_argument("--important-guard", choices=IMPORTANT_GUARD_MODES,
                     default=IMPORTANT_GUARD_DEFAULT, metavar="MODE",
                     help="how Gmail's IMPORTANT label safeguards a sender: "
                          "off | majority (default) | any. Gmail applies it "
                          "automatically, so 'any' immunises nearly every "
                          "high-volume sender. STARRED always guards.")
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

    w = sub.add_parser("ui", help="local web UI: preflight and scan progress")
    w.add_argument("--port", type=int, default=UI_PORT,
                   help="loopback port (default %(default)s); 0 picks a free one")
    w.add_argument("--cache", default="headers.jsonl")
    w.add_argument("--batch", type=int, default=1000)
    w.add_argument("--no-browser", action="store_true",
                   help="do not open a browser; print the URL and wait")
    # No --host. http.server would bind 0.0.0.0 by default, and a flag to
    # re-enable that is a footgun this tool does not need.
    _add_rate_args(w, FETCH_DROPPED, FETCH_STATUS)
    w.set_defaults(func=cmd_ui)

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
