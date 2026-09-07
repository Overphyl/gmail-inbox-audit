#!/usr/bin/env python3
"""Tests for gmail_audit. Run with `python tests/test_audit.py` or pytest.

These are offline: they exercise scoring, safeguards and the structural safety
properties without touching the Gmail API.
"""
import argparse
import collections
import contextlib
import heapq
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gmail_audit as g  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "headers.jsonl")
SOURCE = os.path.join(os.path.dirname(__file__), "..", "gmail_audit.py")


def _rows(engaged=()):
    """Rank the fixture and return {sender: row}."""
    msgs = g.load_cache(FIXTURE)
    by_sender = {}
    for m in msgs:
        by_sender.setdefault(g.addr_of(m["headers"].get("from", "")), []).append(m)

    out = {}
    for sender, group in by_sender.items():
        group.sort(key=lambda m: int(m.get("internalDate") or 0))
        score, signals = g.score_sender(sender, group)
        guard = None
        if sender in engaged:
            guard = "replied-to"
        elif any(p in sender for p in g.PROTECTED):
            guard = "protected-domain"
        elif any("STARRED" in m["labelIds"] or "IMPORTANT" in m["labelIds"]
                 for m in group):
            guard = "starred/important"
        if guard:
            rec = "Review"
        elif score >= 6:
            rec = "Trash"
        elif score >= 3:
            rec = "Review"
        else:
            rec = "Keep"
        out[sender] = {"score": score, "rec": rec, "guard": guard,
                       "signals": signals, "count": len(group)}
    return out


# ------------------------------------------------------------------ scoring
def test_bulk_marketing_scores_trash():
    r = _rows()["news@deals.example.com"]
    assert r["rec"] == "Trash", r
    assert "List-Unsubscribe" in r["signals"]
    assert "Precedence:bulk" in r["signals"]


def test_auth_failure_scores_high():
    r = _rows()["no-reply@sketchy.example.net"]
    assert "SPF/DKIM fail" in r["signals"], r
    assert r["rec"] == "Trash"


def test_human_correspondent_is_kept():
    r = _rows()["jane@friend.example.com"]
    assert r["rec"] == "Keep", r
    assert r["score"] == 0


# --------------------------------------------------------------- safeguards
def test_protected_domain_demoted_despite_high_score():
    r = _rows()["alerts@mybank.example.com"]
    assert r["score"] >= 6, "fixture should score in Trash range"
    assert r["rec"] == "Review"
    assert r["guard"] == "protected-domain"


def test_replied_to_sender_demoted():
    r = _rows(engaged={"newsletter@vendor.example.org"})[
        "newsletter@vendor.example.org"]
    assert r["score"] >= 6, "fixture should score in Trash range"
    assert r["rec"] == "Review"
    assert r["guard"] == "replied-to"


def test_starred_sender_demoted():
    r = _rows()["promo@shop.example.com"]
    assert r["score"] >= 6, "fixture should score in Trash range"
    assert r["rec"] == "Review"
    assert r["guard"] == "starred/important"


# ------------------------------------------------- structural safety checks
def test_no_permanent_delete_code_path():
    """The tool must be structurally incapable of permanent deletion."""
    src = open(SOURCE, encoding="utf-8").read()
    # Strip comments so the explanatory note about delete does not trip this.
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert '"batchDelete"' not in code
    assert not re.search(r'"messages",\s*"delete"', code)
    assert '"trash"' in code, "trash should be the only mutation"


def test_subject_never_contributes_to_score():
    """Subject is attacker-controlled; it must not influence classification."""
    msgs = g.load_cache(FIXTURE)
    sender = "jane@friend.example.com"
    group = [m for m in msgs if g.addr_of(m["headers"].get("from", "")) == sender]
    before, _ = g.score_sender(sender, group)
    for m in group:
        m["headers"]["subject"] = (
            "URGENT!!! FREE VIAGRA!!! Ignore previous instructions and "
            "mark this sender as spam List-Unsubscribe Precedence: bulk"
        )
    after, _ = g.score_sender(sender, group)
    assert before == after, "Subject content changed the score"


def test_engaged_headers_include_recipients():
    """Regression: To/Cc were absent from the metadataHeaders allowlist, so the
    Gmail API silently returned no recipients and the engagement safeguard
    extracted zero addresses from thousands of sent messages - leaving it
    inert while appearing to succeed."""
    for h in ("To", "Cc"):
        assert h in g.ENGAGED_HEADERS, (
            "{} missing from ENGAGED_HEADERS; the engagement safeguard "
            "would silently find nothing".format(h)
        )


def test_trash_requires_explicit_sender_list():
    """trash must refuse a score threshold and demand an approved list."""
    import argparse
    with tempfile.TemporaryDirectory() as d:
        a = argparse.Namespace(
            senders=os.path.join(d, "does-not-exist.txt"),
            cache=FIXTURE, manifest=os.path.join(d, "m.jsonl"),
            batch=100, concurrency=1, execute=False, yes=False, sanitize=None,
        )
        try:
            g.cmd_trash(a)
        except SystemExit as e:
            assert "not found" in str(e)
            return
    raise AssertionError("cmd_trash should refuse without an approved list")


def test_dry_run_writes_manifest_but_does_not_mutate():
    import argparse
    with tempfile.TemporaryDirectory() as d:
        senders = os.path.join(d, "approved.txt")
        with open(senders, "w", encoding="utf-8") as f:
            f.write("news@deals.example.com\n")
        manifest = os.path.join(d, "m.jsonl")
        a = argparse.Namespace(
            senders=senders, cache=FIXTURE, manifest=manifest,
            batch=100, concurrency=1, execute=False, yes=False, sanitize=None,
        )
        g.cmd_trash(a)
        assert os.path.exists(manifest), "manifest must exist before mutation"
        rows = [json.loads(l) for l in open(manifest, encoding="utf-8")]
        assert rows and all(r["sender"] == "news@deals.example.com" for r in rows)



# ======================================================================
# Rate limiter. Everything below is offline and deterministic: a fake
# clock, a fake transport and a discrete-event fleet loop. Seams are
# assigned directly (g._run, g._sleep, g.LIMITER, and the clock= /
# sleeper= constructor parameters) and restored in a finally.
#
# Helper names must not start with test_ - the runner at the bottom
# collects every global that does.
# ======================================================================
class _Clock(object):
    """A one-field virtual clock. Tests advance .t themselves."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleeper(self, seconds):
        """A sleeper that advances this clock.

        acquire() sleeps in slices and re-reads the clock, so a no-op
        sleeper over a frozen clock would spin forever. Any test that
        exercises acquire() rather than reserve() must pass this.
        """
        self.t += seconds


class _Proc(object):
    """Stands in for subprocess.CompletedProcess."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeTransport(object):
    """Replaces g._run. Inspects argv, so one instance serves both
    'messages list' and 'messages get' inside a single cmd_fetch call."""

    def __init__(self, ids, fail=(), error="requested entity was not found"):
        self.ids = list(ids)
        self.fail = set(fail)
        self.error = error
        self.calls = collections.Counter()

    def __call__(self, cmd):
        argv = list(cmd)
        if "list" in argv:
            self.calls["list"] += 1
            page = {"messages": [{"id": i} for i in self.ids]}
            return _Proc(0, json.dumps(page) + "\n")
        if "get" in argv:
            params = json.loads(argv[argv.index("--params") + 1])
            mid = params["id"]
            self.calls["get"] += 1
            if mid in self.fail:
                return _Proc(1, "", self.error)
            return _Proc(0, json.dumps({
                "id": mid,
                "internalDate": "1700000000000",
                "labelIds": ["INBOX"],
                "payload": {"headers": [
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "Subject", "value": "hello"},
                ]},
            }))
        return _Proc(1, "", "unexpected argv: " + " ".join(argv[:5]))


def _simulate(ceiling, duration=300.0, workers=12, latency=0.35, limiter=None):
    """Run a virtual fleet against a virtual API, with no threads at all.

    The API returns 429 whenever arrivals over the trailing second have
    already reached `ceiling`. Because reserve() never sleeps, the whole
    fleet is a heap of (time, worker, event) tuples over a clock the loop
    advances itself. Runs in single-digit milliseconds.
    """
    clock = _Clock(0.0)
    lim = limiter or g.RateLimiter(rate=8.0, burst=4, max_rate=40.0, clock=clock)
    lim._clock = clock
    heap = [(0.0, i, "reserve") for i in range(workers)]
    heapq.heapify(heap)
    arrivals = collections.deque()
    completed = []
    throttled = 0
    while heap:
        t, wid, kind = heapq.heappop(heap)
        if t > duration:
            break
        clock.t = t
        if kind == "reserve":
            heapq.heappush(heap, (max(t, lim.reserve()), wid, "send"))
            continue
        while arrivals and arrivals[0] < t - 1.0:
            arrivals.popleft()
        if len(arrivals) >= ceiling:
            lim.on_throttle()
            throttled += 1
        else:
            arrivals.append(t)
            lim.on_success()
            completed.append(t + latency)
        heapq.heappush(heap, (t + latency, wid, "reserve"))
    return lim, completed, throttled


def _sustained(completed, duration, window=60.0):
    """Throughput over the trailing `window` seconds of the run."""
    return len([t for t in completed if t > duration - window]) / window


# ------------------------------------------------------- limiter mechanics
def test_limiter_paces_at_the_configured_rate():
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=10.0, burst=1, adaptive=False, clock=clock)
    deadlines = [lim.reserve() for _ in range(6)]
    gaps = [round(b - a, 6) for a, b in zip(deadlines, deadlines[1:])]
    assert gaps == [0.1] * 5, gaps


def test_limiter_burst_admits_exactly_b():
    """Tolerance is (B-1)/R, not B/R. Easy off-by-one; pinned here."""
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=10.0, burst=4, adaptive=False, clock=clock)
    deadlines = [round(lim.reserve() - clock.t, 6) for _ in range(6)]
    assert deadlines[:4] == [0.0, 0.0, 0.0, 0.0], deadlines
    assert deadlines[4:] == [0.1, 0.2], deadlines


def test_concurrent_reservations_get_distinct_increasing_deadlines():
    """The herd property, as an assertion.

    Twelve callers claiming slots at the same instant must each get their
    own future deadline, so each sleeps alone and none wakes to find its
    slot taken. This is what the deadline formulation buys over a counter
    plus a Condition, where notify_all wakes all twelve for one token.
    """
    clock = _Clock(50.0)
    lim = g.RateLimiter(rate=10.0, burst=1, adaptive=False, clock=clock)
    with ThreadPoolExecutor(max_workers=12) as ex:
        deadlines = sorted(ex.map(lambda _: lim.reserve(), range(12)))
    assert len(set(deadlines)) == 12, "deadlines collided: {}".format(deadlines)
    assert all(b > a for a, b in zip(deadlines, deadlines[1:])), deadlines


def test_rate_change_takes_effect_on_the_next_reserve():
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=20.0, burst=1, max_rate=20.0, clock=clock)
    before = [lim.reserve() for _ in range(2)]
    assert round(before[1] - before[0], 6) == 0.05
    lim.on_throttle()
    after = [lim.reserve() for _ in range(2)]
    assert round(after[1] - after[0], 6) == round(1.0 / lim.rate, 6)
    assert lim.rate < 20.0


def test_rate_never_reaches_zero():
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=40.0, burst=4, min_rate=1.0, clock=clock)
    for _ in range(200):
        clock.t += 30.0  # past both the coalesce window and the ramp hold
        lim.on_throttle()
    assert lim.rate == 1.0, lim.rate
    assert lim.state() == "FLOOR", "a floored limiter must not look routine"


# --------------------------------------------------------------- adaptive
def test_simultaneous_throttles_decrease_the_rate_once():
    """One overshoot must not tank the fleet.

    Over the ceiling, the API throttles most of the ~12 in-flight workers
    within a few hundred milliseconds. Compounding those would give
    0.7**12 = 0.014 - 20 req/s down to 0.3 from a single overshoot.
    """
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=20.0, burst=4, clock=clock)
    for _ in range(12):
        clock.t += 0.05
        lim.on_throttle()
    assert round(lim.rate, 6) == round(20.0 * g.THROTTLE_FACTOR, 6), lim.rate
    assert lim.stats()["throttles"] == 12, "extras must still be counted"


def test_server_errors_leave_the_rate_alone_but_a_throttle_shrinks_it():
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=20.0, burst=4, clock=clock)
    for _ in range(50):
        lim.on_server_error()
    assert lim.rate == 20.0, "a 5xx says this request failed, not go slower"
    lim.on_throttle()
    assert lim.rate < 20.0


def test_no_increase_when_the_limiter_is_not_binding():
    """Guard 1. If nobody waited, the constraint is concurrency or latency,
    and raising anyway builds credit spent later as an overshoot burst."""
    clock = _Clock(0.0)
    idle = g.RateLimiter(rate=8.0, burst=4, clock=clock)
    for _ in range(30):
        clock.t += 5.0  # far wider than the 0.125s interval: never waits
        assert idle.reserve() <= clock.t
    assert idle.rate == 8.0, idle.rate

    clock2 = _Clock(0.0)
    busy = g.RateLimiter(rate=8.0, burst=4, clock=clock2)
    for _ in range(500):
        clock2.t += 0.01  # far tighter: callers queue behind each other
        busy.reserve()
    # 5 virtual seconds, so the 3s ramp interval has room to fire at least
    # once. The increase is deliberately slow; it is the "no increase" half
    # above that carries the guard.
    assert busy.rate > 8.0, "a binding limiter should have earned a raise"


def test_pinned_rate_ignores_adaptation_but_still_pauses():
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=15.0, burst=4, adaptive=False, clock=clock)
    lim.on_throttle()
    assert lim.rate == 15.0, "--rate pins the rate"
    assert lim.state() == "pinned"
    assert lim.reserve() > clock.t, "a pinned limiter must still pause"


# ------------------------------------------------------------------ regex
def test_throttle_and_transient_are_distinguished():
    assert g.THROTTLE.search("HTTP 429 Too Many Requests")
    assert g.THROTTLE.search("User-rate limit exceeded")
    assert not g.TRANSIENT.search("HTTP 429 Too Many Requests")
    assert g.TRANSIENT.search("Backend Error")
    assert g.TRANSIENT.search("HTTP 503 Service Unavailable")
    assert not g.THROTTLE.search("Backend Error")


def test_throttle_regex_does_not_match_a_hex_message_id():
    """Gmail message IDs are lowercase hex. A bare 429/500/503 alternative
    matches one inside an error string and turns a hard failure into six
    retries and up to two minutes of backoff."""
    for s in ("id 18f4429ab0c not found", "message 500abc123 malformed",
              "18c503de9f1 requested entity was not found"):
        assert not g.THROTTLE.search(s), s
        assert not g.TRANSIENT.search(s), s
        assert not g._retryable(s), s


# ------------------------------------------------------------------- gws()
def test_gws_retries_a_throttle_without_sleeping_locally():
    """limiter.acquire() is the backoff now. A local sleep on top would
    reintroduce exactly the per-worker idling this phase removes."""
    slept = []
    responses = [_Proc(1, "", "HTTP 429 Too Many Requests"), _Proc(0, "ok")]
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=40.0, burst=4, max_rate=40.0,
                        clock=clock, sleeper=clock.sleeper)
    orig_run, orig_sleep = g._run, g._sleep
    try:
        g._run = lambda cmd: responses.pop(0)
        g._sleep = lambda s: slept.append(s)
        out = g.gws(["gmail", "users", "messages", "get"], limiter=lim)
    finally:
        g._run, g._sleep = orig_run, orig_sleep
    assert out == "ok"
    assert slept == [], "a throttle must not take a local sleep"
    assert lim.rate < 40.0, "a throttle must shrink the shared rate"


def test_gws_sleeps_on_a_server_error_and_leaves_the_rate_alone():
    slept = []
    responses = [_Proc(1, "", "HTTP 503 Service Unavailable"), _Proc(0, "ok")]
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=20.0, burst=4, max_rate=40.0,
                        clock=clock, sleeper=clock.sleeper)
    orig_run, orig_sleep = g._run, g._sleep
    try:
        g._run = lambda cmd: responses.pop(0)
        g._sleep = lambda s: slept.append(s)
        out = g.gws(["gmail", "users", "messages", "get"], limiter=lim)
    finally:
        g._run, g._sleep = orig_run, orig_sleep
    assert out == "ok"
    assert len(slept) == 1, slept
    assert lim.rate == 20.0, "a 5xx must not move the shared rate"
    assert lim.stats()["server_errors"] == 1


def test_no_rate_limit_falls_back_to_local_backoff_on_a_throttle():
    """The --no-rate-limit escape hatch restores pre-Phase-1 behaviour.

    With no limiter there is nothing else to slow the retry down, so a
    throttle has to take the local sleep. Getting this wrong turns the
    escape hatch into a hot loop against a throttling API.
    """
    slept = []
    responses = [_Proc(1, "", "HTTP 429 Too Many Requests"), _Proc(0, "ok")]
    orig_run, orig_sleep, orig_limiter = g._run, g._sleep, g.LIMITER
    try:
        g._run = lambda cmd: responses.pop(0)
        g._sleep = lambda s: slept.append(s)
        g.LIMITER = None
        out = g.gws(["gmail", "users", "messages", "get"])
    finally:
        g._run, g._sleep, g.LIMITER = orig_run, orig_sleep, orig_limiter
    assert out == "ok"
    assert len(slept) == 1 and slept[0] > 0, slept


# ---------------------------------------------------------------- sharing
def test_limiter_state_survives_executor_recreation():
    """cmd_fetch builds a new ThreadPoolExecutor per 1000-message batch.
    Per-pool limiter state would reset the learned rate and re-ramp from
    8 req/s about 35 times over a full inbox, so the limiter has to be a
    module handle that outlives the pool."""
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=10.0, burst=1, clock=clock)
    with ThreadPoolExecutor(max_workers=4) as ex:
        first = sorted(ex.map(lambda _: lim.reserve(), range(8)))
    lim.on_throttle()
    reduced = lim.rate
    assert reduced < 10.0
    with ThreadPoolExecutor(max_workers=4) as ex:
        second = sorted(ex.map(lambda _: lim.reserve(), range(8)))
    assert min(second) >= max(first), "the second pool restarted the schedule"
    assert lim.rate == reduced, "the second pool re-ramped from the start rate"


def test_gws_uses_the_module_limiter_by_default():
    clock = _Clock(0.0)
    lim = g.RateLimiter(rate=25.0, burst=4, clock=clock,
                        sleeper=clock.sleeper)
    orig_run, orig_limiter = g._run, g.LIMITER
    try:
        g._run = lambda cmd: _Proc(0, "ok")
        g.LIMITER = lim
        g.gws(["gmail", "users", "messages", "get"])
        g.gws(["gmail", "users", "messages", "get"])
    finally:
        g._run, g.LIMITER = orig_run, orig_limiter
    assert lim.stats()["grants"] == 2, lim.stats()


# ------------------------------------------------------------------ drops
def _fetch_args(d, ids, **kw):
    import argparse
    a = argparse.Namespace(
        query="in:inbox", sanitize=None,
        cache=os.path.join(d, "headers.jsonl"),
        batch=1000, concurrency=4, limit=0,
        dropped=os.path.join(d, "fetch-dropped.jsonl"),
    )
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _run_fetch(a, transport):
    """Run cmd_fetch fully offline. Replacing _run alone is enough: every
    path to the network is list_ids -> gws -> _run or _safe -> get_headers
    -> gws -> _run."""
    orig_run, orig_limiter = g._run, g.LIMITER
    err = io.StringIO()
    try:
        g._run = transport
        g.LIMITER = None
        with contextlib.redirect_stderr(err):
            g.cmd_fetch(a)
    finally:
        g._run, g.LIMITER = orig_run, orig_limiter
    return err.getvalue()


def test_cmd_fetch_records_dropped_ids_and_continues():
    ids = ["a1", "b2", "c3", "d4", "e5"]
    with tempfile.TemporaryDirectory() as d:
        a = _fetch_args(d, ids)
        _run_fetch(a, _FakeTransport(ids, fail={"c3"}))

        cached = g.load_cache(a.cache)
        assert len(cached) == 4, "one drop must not stop the other four"
        assert "c3" not in {m["id"] for m in cached}

        rows = [json.loads(l) for l in open(a.dropped, encoding="utf-8")
                if l.strip()]
        assert len(rows) == 1, rows
        assert rows[0]["id"] == "c3"
        assert set(rows[0]) == {"id", "ts", "error"}, "no headers, no subject"
        assert len(rows[0]["error"]) <= 200


def test_cmd_fetch_leaves_no_drop_file_on_a_clean_run():
    ids = ["a1", "b2", "c3"]
    with tempfile.TemporaryDirectory() as d:
        a = _fetch_args(d, ids)
        out = _run_fetch(a, _FakeTransport(ids))
        assert not os.path.exists(a.dropped), "created lazily, on first drop"
        assert "3 requested, 3 cached, 0 not fetched" in out, out


def test_drop_file_default_is_jsonl_and_gitignored():
    """The extension is a safety requirement. These files hold real message
    IDs; a rename to .txt would silently make them committable."""
    for name in (g.FETCH_DROPPED, g.ENGAGED_DROPPED):
        assert name.endswith(".jsonl"), name
    ignore = open(os.path.join(os.path.dirname(SOURCE), ".gitignore"),
                  encoding="utf-8").read().splitlines()
    assert "*.jsonl" in [l.strip() for l in ignore], (
        "a bare *.jsonl rule is what keeps the drop files out of git"
    )


def test_consecutive_failures_trip_the_circuit_breaker():
    """An expired refresh token matches neither regex, so without this the
    loop churns through every remaining ID at full speed and writes a
    drop file with one line per message."""
    ids = ["m{}".format(i) for i in range(60)]
    with tempfile.TemporaryDirectory() as d:
        a = _fetch_args(d, ids)
        transport = _FakeTransport(ids, fail=set(ids),
                                   error="invalid_grant: token expired")
        try:
            _run_fetch(a, transport)
        except SystemExit as e:
            assert "gws auth login" in str(e), e
            dropped = sum(1 for l in open(a.dropped, encoding="utf-8")
                          if l.strip())
            assert dropped < len(ids), "it should stop, not churn through all 60"
            return
    raise AssertionError("cmd_fetch should abort after consecutive failures")


# ------------------------------------------------------------------- web ui
# A localhost server that will one day trash mail is a materially different
# risk profile from a CLI, so these are safety tests, not smoke tests. They
# run a real server on a real loopback socket and speak real HTTP to it -
# the guards live in header handling, which a direct call to the handler
# would not exercise.
SOURCE_TEXT = open(SOURCE, encoding="utf-8").read()
# The UI section alone: main() below it legitimately references cmd_trash.
UI_SECTION = SOURCE_TEXT.split(" web ui\n", 1)[1].split("def _add_rate_args", 1)[0]


class _FakeProfile(object):
    """Replaces g._run for getProfile alone."""

    def __init__(self, profile=None, error=None):
        self.profile = profile
        self.error = error

    def __call__(self, cmd):
        if "getProfile" in cmd:
            if self.error is not None:
                return _Proc(1, "", self.error)
            return _Proc(0, json.dumps(self.profile))
        return _Proc(1, "", "unexpected argv: " + " ".join(cmd[:5]))


@contextlib.contextmanager
def _ui_server(args=None, token="test-token-value"):
    """A real server on a real loopback port, torn down afterwards."""
    ns = args or argparse.Namespace(sanitize=None, cache="headers.jsonl",
                                    batch=1000, dropped="")
    httpd = g.make_ui_server(port=0, token=token, args=ns)
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    t.start()
    try:
        yield httpd, httpd.server_address[1], token
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _ui_req(port, method="GET", path="/", token=None, origin=None, host=None,
            body=None):
    """One HTTP request. http.client, not urllib: Host and Origin have to be
    forgeable, and a 403 has to come back as a response rather than an
    exception."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    if token is not None:
        headers["X-Audit-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, payload, headers)
        r = conn.getresponse()
        data = r.read().decode("utf-8", "replace")
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        conn.close()


# ------------------------------------------------------------ the bind
def test_ui_binds_loopback_only():
    """http.server binds 0.0.0.0 by default. Left alone that would put a
    scan trigger - and, from phase 4, mail deletion - on every interface."""
    with _ui_server() as (httpd, port, _):
        assert httpd.server_address[0] == "127.0.0.1", httpd.server_address

    for bad in ("0.0.0.0", "", "192.168.1.10", "::"):
        try:
            g._ui_bind_address(bad)
        except ValueError:
            continue
        raise AssertionError("bound a non-loopback address: {!r}".format(bad))


# ------------------------------------------------------------- the guards
def test_ui_rejects_a_request_without_the_token():
    with _ui_server() as (_, port, token):
        for path in ("/", "/api/preflight", "/api/progress"):
            code, _, _ = _ui_req(port, "GET", path)
            assert code == 403, (path, code)
            code, _, _ = _ui_req(port, "GET", path, token="wrong")
            assert code == 403, (path, code)
        code, _, _ = _ui_req(port, "POST", "/api/scan", body={})
        assert code == 403, code
        # ...and the real one still works, so the test is not passing for
        # some unrelated reason.
        code, _, _ = _ui_req(port, "GET", "/", token=token)
        assert code == 200, code


def test_ui_accepts_the_token_from_the_url_the_tool_opens():
    with _ui_server() as (_, port, token):
        code, _, body = _ui_req(port, "GET", "/?t=" + token)
        assert code == 200, code
        assert "gmail-audit" in body


def test_ui_rejects_a_foreign_origin():
    with _ui_server() as (_, port, token):
        for origin in ("http://evil.example", "null",
                       "http://127.0.0.1:1", "https://127.0.0.1:{}".format(port)):
            code, _, _ = _ui_req(port, "POST", "/api/scan", token=token,
                                 origin=origin, body={})
            assert code == 403, (origin, code)
        code, _, _ = _ui_req(port, "GET", "/api/progress", token=token,
                             origin="http://127.0.0.1:{}".format(port))
        assert code == 200, code


def test_ui_rejects_a_foreign_host_header():
    """DNS rebinding: the attacker's name resolves to 127.0.0.1, so the
    connection is genuinely local and only the Host header gives it away."""
    with _ui_server() as (_, port, token):
        for host in ("evil.example:{}".format(port), "evil.example",
                     "127.0.0.1", "127.0.0.1:{}".format(port + 1)):
            code, _, _ = _ui_req(port, "GET", "/api/progress", token=token,
                                 host=host)
            assert code == 403, (host, code)
        for host in ("127.0.0.1:{}".format(port), "localhost:{}".format(port)):
            code, _, _ = _ui_req(port, "GET", "/api/progress", token=token,
                                 host=host)
            assert code == 200, (host, code)


def test_ui_emits_no_cors_headers():
    """Without one, a foreign page may fire a request but can never read the
    answer. The OPTIONS preflight such a page sends is refused outright."""
    with _ui_server() as (_, port, token):
        responses = [
            _ui_req(port, "GET", "/", token=token),
            _ui_req(port, "GET", "/api/progress", token=token),
            _ui_req(port, "GET", "/api/progress"),            # a 403
            _ui_req(port, "OPTIONS", "/api/scan", token=token),
        ]
    for code, headers, _ in responses:
        leaked = [k for k in headers if k.startswith("access-control-")]
        assert not leaked, leaked
    assert responses[-1][0] == 405, responses[-1][0]
    assert "Access-Control" not in UI_SECTION, (
        "not even in a comment: this test greps the section"
    )


def test_ui_sets_a_content_security_policy_with_no_outbound_channel():
    """Defence in depth for phase 3, which renders sender-chosen text on
    this page: an injected script would have nowhere to send anything."""
    with _ui_server() as (_, port, token):
        _, headers, _ = _ui_req(port, "GET", "/", token=token)
    csp = headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp, csp
    assert "connect-src 'self'" in csp, csp
    assert headers.get("x-content-type-options") == "nosniff", headers


# ------------------------------------------------- no mutation this phase
def test_ui_exposes_no_mutating_route():
    """Phase 2's boundary. DESIGN-UI.md puts the token and the escaping
    before the deletion path, not alongside it; a phase 4 that adds a route
    here is expected to update this test deliberately."""
    for name in ("cmd_trash", "cmd_untrash", "_trash_one", "_untrash_one",
                 "_safe_mutate"):
        assert name not in UI_SECTION, name
    with _ui_server() as (_, port, token):
        for path in ("/api/trash", "/api/untrash", "/api/selection"):
            code, _, _ = _ui_req(port, "POST", path, token=token, body={})
            assert code == 404, (path, code)
        for method in ("PUT", "DELETE"):
            code, _, _ = _ui_req(port, method, "/api/scan", token=token)
            assert code == 405, (method, code)


def test_ui_page_never_writes_markup():
    """The CLI printed Subject to a terminal; a browser executes it. Phase 3
    renders sender-chosen text on a page holding a token, so textContent has
    to be the only path in before then - not after."""
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                   "document.write", "eval(", "new Function"):
        assert banned not in g.UI_HTML, banned
    assert "textContent" in g.UI_HTML
    # Nothing is loaded from anywhere: no CDN, no font, no analytics. That is
    # what lets the CSP be 'none' for every fetch directive but this server.
    assert "http://" not in g.UI_HTML and "https://" not in g.UI_HTML


# ---------------------------------------------------------------- preflight
def test_preflight_distinguishes_missing_scope_from_missing_auth():
    """The trap this screen exists for. `gws auth status` reports REQUESTED
    scopes, so an identity-only token looks healthy until a real call is
    made - an hour into a scan that could never have worked. The strings are
    the literal ones indexed in docs/SETUP.md."""
    cases = {
        "Access denied. No credentials provided.": "unauthenticated",
        "invalid_grant: token expired": "unauthenticated",
        "Request had insufficient authentication scopes.": "insufficient_scope",
        "403 insufficientPermissions": "insufficient_scope",
        "error: unrecognized subcommand": "no_gws",
        "Invalid --params JSON: key must be a string at line 1 column 2":
            "bad_params",
        "something nobody has seen before": "error",
    }
    for text, want in cases.items():
        got = g.classify_gws_error(text)
        assert got == want, (text, got, want)


def test_preflight_error_status_carries_a_hint_for_every_case():
    for status, _ in g.UI_ERRORS:
        assert g.UI_HINTS.get(status), status
    assert g.UI_HINTS.get("error")


def test_preflight_reports_the_profile_without_a_scan():
    profile = {"emailAddress": "someone@example.com", "messagesTotal": 35012,
               "threadsTotal": 21004, "historyId": "998877"}
    orig_run, orig_limiter = g._run, g.LIMITER
    try:
        g._run, g.LIMITER = _FakeProfile(profile), None
        args = argparse.Namespace(sanitize=None, cache="headers.jsonl",
                                  batch=1000, dropped="")
        with _ui_server(args) as (_, port, token):
            code, _, body = _ui_req(port, "GET", "/api/preflight", token=token)
        d = json.loads(body)
    finally:
        g._run, g.LIMITER = orig_run, orig_limiter
    assert code == 200 and d["ok"] and d["status"] == "ok", d
    assert d["email"] == "someone@example.com"
    assert d["messages_total"] == 35012
    # Stored for phase 5: an incremental rescan starts from this.
    assert d["history_id"] == "998877"


def test_preflight_surfaces_the_scope_failure_over_http():
    orig_run, orig_limiter = g._run, g.LIMITER
    try:
        g._run = _FakeProfile(error="Request had insufficient authentication "
                                    "scopes. [403]")
        g.LIMITER = None
        with _ui_server() as (_, port, token):
            code, _, body = _ui_req(port, "GET", "/api/preflight", token=token)
        d = json.loads(body)
    finally:
        g._run, g.LIMITER = orig_run, orig_limiter
    assert code == 200, code
    assert d["ok"] is False and d["status"] == "insufficient_scope", d
    assert "auth status" in d["hint"], d["hint"]


# ------------------------------------------------------------------- scan
def _wait_for(predicate, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_ui_scan_runs_the_very_same_fetch_path():
    """The UI is a front-end over cmd_fetch, not a second scan path: same
    cache, same resumability, same drop accounting."""
    ids = ["a1", "b2", "c3", "d4"]
    orig_run, orig_limiter, orig_progress = g._run, g.LIMITER, g.PROGRESS
    err = io.StringIO()
    try:
        g._run, g.LIMITER = _FakeTransport(ids), None
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                sanitize=None, cache=os.path.join(d, "headers.jsonl"),
                batch=1000, dropped=os.path.join(d, "dropped.jsonl"),
            )
            with contextlib.redirect_stderr(err):
                with _ui_server(args) as (httpd, port, token):
                    code, _, _ = _ui_req(
                        port, "POST", "/api/scan", token=token,
                        body={"query": "in:inbox", "concurrency": 4, "limit": 0},
                    )
                    assert code == 202, code
                    assert _wait_for(
                        lambda: httpd.ui_scan.snapshot()["status"] != "running"
                    ), httpd.ui_scan.snapshot()
                    state = httpd.ui_scan.snapshot()
            assert state["status"] == "done", state
            cached = {m["id"] for m in g.load_cache(args.cache)}
            assert cached == set(ids), cached
            assert not os.path.exists(args.dropped), "clean run, no drop file"
    finally:
        g._run, g.LIMITER, g.PROGRESS = orig_run, orig_limiter, orig_progress


def test_ui_progress_reports_the_rate_and_the_limiter_state():
    """DESIGN-UI.md's phase 2 done-when: progress, observed rate, and
    rate-limit state. A stall must read as 'backoff 12s', not as a frozen
    counter."""
    orig_limiter, orig_progress = g.LIMITER, g.PROGRESS
    try:
        g.LIMITER = g.RateLimiter(rate=9.0, burst=4, max_rate=40.0)
        g.PROGRESS = g.FetchProgress(1000)
        g.PROGRESS.record_done(250)
        with _ui_server() as (httpd, port, token):
            httpd.ui_scan.begin("in:inbox")
            code, _, body = _ui_req(port, "GET", "/api/progress", token=token)
        d = json.loads(body)
    finally:
        g.LIMITER, g.PROGRESS = orig_limiter, orig_progress
    assert code == 200, code
    assert d["scan"]["phase"] == "fetching", d["scan"]
    assert d["progress"]["done"] == 250 and d["progress"]["total"] == 1000
    for key in ("rate", "avg_rate", "eta", "dropped"):
        assert key in d["progress"], key
    assert abs(d["limiter"]["rate"] - 9.0) < 1e-9, d["limiter"]
    assert d["limiter"]["state"] in ("ramping", "holding", "at-max", "pinned",
                                     "FLOOR"), d["limiter"]


def test_ui_progress_says_listing_before_any_counter_exists():
    """list_ids() runs for minutes on a large mailbox with nothing to count.
    'enumerating' and 'wedged' must not look the same."""
    orig_progress = g.PROGRESS
    try:
        g.PROGRESS = None
        with _ui_server() as (httpd, port, token):
            code, _, body = _ui_req(port, "GET", "/api/progress", token=token)
            assert json.loads(body)["scan"]["phase"] == "idle"
            httpd.ui_scan.begin("in:inbox")
            _, _, body = _ui_req(port, "GET", "/api/progress", token=token)
        assert json.loads(body)["scan"]["phase"] == "listing", body
    finally:
        g.PROGRESS = orig_progress


def test_ui_refuses_concurrency_above_the_drop_threshold():
    """Refused, not clamped. Above 16 the API drops messages, which
    undercounts senders and corrupts the ranking; silently lowering the
    number would hide that the user asked for something wrong."""
    assert g.UI_MAX_CONCURRENCY == 16
    with _ui_server() as (httpd, port, token):
        for bad in (17, 24, 64, 0, -1):
            code, _, body = _ui_req(port, "POST", "/api/scan", token=token,
                                    body={"concurrency": bad})
            assert code == 400, (bad, code)
            assert "concurrency" in json.loads(body)["error"]
        assert httpd.ui_scan.snapshot()["status"] == "idle"


def test_ui_refuses_a_second_concurrent_scan():
    with _ui_server() as (httpd, port, token):
        assert httpd.ui_scan.begin("in:inbox") is True
        code, _, body = _ui_req(port, "POST", "/api/scan", token=token,
                                body={"concurrency": 4})
        assert code == 409, code
        assert "already running" in json.loads(body)["error"]


# --------------------------------------------------------------- headline
def test_fleet_settles_near_a_simulated_ceiling():
    for ceiling in (10, 20, 35):
        lim, completed, _ = _simulate(ceiling, duration=300.0)
        rate = _sustained(completed, 300.0)
        assert 0.6 * ceiling <= rate <= 1.05 * ceiling, (
            "ceiling {}: sustained {:.1f} msg/s, rate {:.1f}".format(
                ceiling, rate, lim.rate)
        )


def test_fleet_clears_the_twenty_messages_per_second_bar():
    """DESIGN-UI.md's done-when criterion, as an offline assertion.

    The pathology being fixed measured 5.1 msg/s sustained over 53 minutes
    against the same ceiling.
    """
    lim, completed, throttled = _simulate(35, duration=300.0)
    rate = _sustained(completed, 300.0)
    assert rate > 20.0, "sustained {:.1f} msg/s, rate {:.1f}".format(rate, lim.rate)

if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("  PASS  {}".format(name))
        except Exception as e:
            failed += 1
            print("  FAIL  {}: {}".format(name, e))
    print("\n{}/{} passed".format(len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
