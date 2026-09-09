#!/usr/bin/env python3
"""Tests for gmail_audit. Run with `python tests/test_audit.py` or pytest.

These are offline: they exercise scoring, safeguards and the structural safety
properties without touching the Gmail API.
"""
import argparse
import ast
import collections
import contextlib
import datetime
import fnmatch
import heapq
import http.client
import inspect
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

# abspath, not just join: on Python 3.8 __file__ is RELATIVE when the suite is
# run as `python tests/test_audit.py`, which is how CI runs it (3.9 made it
# absolute). Several tests chdir into a temp directory, and a relative fixture
# path stops resolving the moment they do. Four tests failed on the 3.8 floor
# job and nowhere else, which is the entire reason that job exists.
FIXTURE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "fixtures", "headers.jsonl"))
SOURCE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "gmail_audit.py"))


def _rows(engaged=(), important=g.IMPORTANT_GUARD_DEFAULT):
    """Rank the fixture and return {sender: row}.

    Through rank_rows, deliberately: this helper used to reimplement the
    scoring and safeguard logic, so the safeguard tests could pass against a
    copy while the real ranking regressed.
    """
    return {r["sender"]: r
            for r in g.rank_rows(g.load_cache(FIXTURE), engaged, important)}


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
    assert r["guard"] == "starred"


def test_starring_guards_in_every_important_mode():
    """A star is a decision the human made. --important-guard is about the
    label Gmail applies on its own and must not reach this one."""
    for mode in g.IMPORTANT_GUARD_MODES:
        r = _rows(important=mode)["promo@shop.example.com"]
        assert r["guard"] == "starred", (mode, r)
        assert r["rec"] == "Review", (mode, r)


def test_important_on_a_minority_does_not_guard_by_default():
    """The measured failure. Gmail applies IMPORTANT automatically, so under
    `any` a sender is immune once ONE message in their history was flagged -
    which for a high-volume sender is a near certainty regardless of how well
    calibrated the label is. On the first real mailbox this ran against, that
    rule immunised 100% of senders with 100+ messages."""
    sender = "digest@bulk.example.com"  # 2 of 20 flagged
    assert _rows(important="any")[sender]["guard"] == "important"
    assert _rows(important="any")[sender]["rec"] == "Review"
    for mode in ("majority", "off"):
        r = _rows(important=mode)[sender]
        assert r["score"] >= 6, "fixture should score in Trash range"
        assert r["guard"] is None, (mode, r)
        assert r["rec"] == "Trash", (mode, r)


def test_important_on_a_majority_guards_unless_switched_off():
    sender = "updates@service.example.com"  # 9 of 12 flagged
    for mode in ("majority", "any"):
        r = _rows(important=mode)[sender]
        assert r["guard"] == "important", (mode, r)
        assert r["rec"] == "Review", (mode, r)
    r = _rows(important="off")[sender]
    assert r["score"] >= 6, "fixture should score in Trash range"
    assert r["guard"] is None, r
    assert r["rec"] == "Trash", r


def test_the_important_default_is_majority():
    """Belt and braces: the default is the whole point of the change, and a
    caller that forgets to pass the mode must not silently get `any` back."""
    assert g.IMPORTANT_GUARD_DEFAULT == "majority"
    assert _rows()["digest@bulk.example.com"]["rec"] == "Trash"
    assert _rows()["updates@service.example.com"]["rec"] == "Review"


def test_important_coverage_is_shown_even_when_it_guards_nothing():
    """Dropping a safeguard silently is how you get a surprise at trash time.
    The row has to say what --important-guard had to work with."""
    off = _rows(important="off")["digest@bulk.example.com"]
    assert "important:2/20" in g.row_notes(off), g.row_notes(off)
    # ...and it is never spelled twice on a row it did guard.
    guarded = _rows()["updates@service.example.com"]
    notes = g.row_notes(guarded)
    assert "important:9/12" in notes and "important" not in notes, notes


def test_a_safeguard_does_not_promote_a_keep_into_the_review_pile():
    """A guard exists to stop a Trash recommendation. A sender scoring below
    the Trash threshold was never going to get one, so guarding them changes
    no outcome and only lengthens the list a human reads. On the first real
    mailbox, checking the guard before the score moved 1,303 sub-threshold
    senders into Review: 38% of the pile, none of them at any risk."""
    sender = "jane@friend.example.com"
    plain = _rows()[sender]
    assert plain["score"] < 3 and plain["rec"] == "Keep", plain
    guarded = _rows(engaged={sender})[sender]
    assert guarded["guard"] == "replied-to", guarded
    assert guarded["rec"] == "Keep", (
        "a guard below the Trash threshold must not promote the row")


def test_a_guarded_keep_still_carries_the_flag_and_stays_unpreselectable():
    """Not calling a sender out for review is not the same as forgetting they
    are safeguarded. The [!] and the --preselect-score refusal both survive."""
    sender = "jane@friend.example.com"
    rows = _review_rows(engaged={sender})
    row = next(r for r in rows if r["sender"] == sender)
    assert row["rec"] == "Keep" and row["guard"] == "replied-to", row
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        g.write_review(p, rows, preselect_score=0)  # mark everything it can
        line = next(l for l in open(p, encoding="utf-8") if sender in l)
        marks = _marks(p)
    assert g.REVIEW_GUARD_FLAG in line, line
    assert marks[sender] is False, "a safeguarded row is never pre-marked"


def test_a_safeguard_still_holds_at_the_trash_boundary():
    """The half that matters. Every guard must still stop a Trash-scoring
    sender, in both directions of the change."""
    for sender, engaged in (
        ("alerts@mybank.example.com", ()),                       # protected
        ("promo@shop.example.com", ()),                          # starred
        ("updates@service.example.com", ()),                     # important
        ("news@deals.example.com", {"news@deals.example.com"}),  # replied-to
    ):
        r = _rows(engaged=engaged)[sender]
        assert r["score"] >= 6, (sender, r)
        assert r["guard"], (sender, r)
        assert r["rec"] == "Review", (sender, r)


# ------------------------------------------------- structural safety checks
def test_the_suite_uses_absolute_paths_for_its_own_files():
    """Several tests chdir into a temp directory. On Python 3.8 __file__ is
    relative when the suite is run as `python tests/test_audit.py`, so a
    fixture path built from it stops resolving the moment they do. Asserting
    the property is cheaper than rediscovering it on the floor job."""
    for p in (FIXTURE, SOURCE):
        assert os.path.isabs(p), p
        assert os.path.exists(p), p


def test_every_text_file_names_its_encoding():
    """Windows defaults to cp1252, and this tool's files are full of non-ASCII:
    sender display names, subjects, the manifest written with
    ensure_ascii=False. One open() without encoding="utf-8" is a
    UnicodeDecodeError on somebody's mailbox and nobody else's. The rule is
    already documented for subprocess output; it applies just as much to the
    files the tool writes and reads back."""
    tree = ast.parse(open(SOURCE, encoding="utf-8").read())
    missing = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "open"):
            continue
        mode = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value or ""
        if "b" in mode:
            continue  # bytes have no encoding to get wrong
        if "encoding" not in {k.arg for k in node.keywords}:
            missing.append(node.lineno)
    assert not missing, (
        "open() without encoding= at line(s) {}; cp1252 is the Windows "
        "default and these files are not ASCII".format(missing))


def test_no_permanent_delete_code_path():
    """The tool must be structurally incapable of permanent deletion."""
    src = open(SOURCE, encoding="utf-8").read()
    # Strip comments so the explanatory note about delete does not trip this.
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert '"batchDelete"' not in code
    assert not re.search(r'"messages",\s*"delete"', code)
    assert '"trash"' in code, "trash should be the only mutation"


def test_modify_is_add_only():
    """messages.modify joined the permitted mutations on 2026-09-09 for one
    job: restoring the INBOX label that messages.untrash does not put back.
    It may add a label and must never remove one. The rule it widens is about
    permanent deletion, and adding a label cannot delete anything - which only
    stays true while this holds."""
    src = open(SOURCE, encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#"))
    assert "removeLabelIds" not in code, (
        "modify is add-only; removing a label is not something this tool does")


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
            # This test is about the manifest, not the engaged-list guard.
            engaged="", allow_missing_engaged=True,
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


def _parse_fetch(argv):
    """The namespace the real CLI would hand cmd_fetch for `fetch <argv>`.

    Through the actual parser, so a default that only exists in a test cannot
    disagree with the one a user gets.
    """
    orig = sys.argv
    try:
        sys.argv = ["gmail_audit.py", "fetch"] + list(argv)
        parser = None
        captured = {}

        def fake_fetch(a):
            captured["a"] = a

        real = g.cmd_fetch
        g.cmd_fetch = fake_fetch
        try:
            try:
                g.main()
            except SystemExit:
                raise
        finally:
            g.cmd_fetch = real
        return captured["a"]
    finally:
        sys.argv = orig


def test_a_scan_pins_its_rate_by_default():
    """The adaptive controller is measurably worse than a fixed rate on a real
    mailbox: it collapses to the 1.0 floor and manages 3.66 msg/s where pinned
    at 8 held 5.17. Until that is fixed, the broken half is opt-in rather than
    the thing every first run gets."""
    a = _parse_fetch([])
    assert a.rate is None and a.adaptive is False, a
    lim = g._make_limiter(a)
    assert lim.rate == g.RATE_DEFAULT, lim.rate
    assert lim.adaptive is False, "the default must not search for a rate"


def test_adaptive_is_still_reachable_two_ways():
    """It is deprecated, not removed. --rate 0 has always meant 'adapt'."""
    for argv in (["--adaptive"], ["--rate", "0"]):
        lim = g._make_limiter(_parse_fetch(argv))
        assert lim.adaptive is True, argv
        assert lim.rate == g.RATE_START, (argv, lim.rate)


def test_rate_and_adaptive_cannot_both_be_asked_for():
    """Silently letting one win is how a run ends up paced by something the
    operator did not choose. The refusal must also say what to type instead:
    since --rate carries a default, "pin at 8 and also adapt" is a reasonable
    thing to have believed you were asking for, and --start-rate is not a name
    anyone guesses."""
    try:
        g._make_limiter(_parse_fetch(["--rate", "12", "--adaptive"]))
    except SystemExit as e:
        assert "--start-rate 12" in str(e), e
        assert "--rate 12" in str(e), e
        return
    raise AssertionError("--rate and --adaptive must be mutually exclusive")


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


def test_a_precondition_failure_is_transient_not_a_throttle():
    """A restore untrashes then adds INBOX back, and Gmail occasionally has not
    committed the untrash when the modify arrives. Measured once in 1,108
    messages; the retry succeeded by hand, and the message had no SPAM, DRAFT
    or TRASH label to explain a permanent refusal. Before this it matched
    neither pattern and got zero retries."""
    for text in ("error[api]: Precondition check failed.",
                 "FAILED_PRECONDITION: message is not in trash",
                 "failed precondition"):
        assert g.TRANSIENT.search(text), text
        assert not g.THROTTLE.search(text), (
            "it is one request losing a race, not the fleet being too fast - "
            "shrinking the shared rate would be the wrong response")


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


# ------------------------------------------------------------------ status
def _gitignored(name):
    """True if some .gitignore rule matches this filename.

    Matched rather than looked up literally: a rename that escapes the rules
    is exactly the failure this guards against, and a rule the file no longer
    matches would still be present in the list.
    """
    path = os.path.join(os.path.dirname(SOURCE), ".gitignore")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            if fnmatch.fnmatch(name, line.rstrip("/")):
                return True
    return False


def test_status_file_carries_a_scan_across_processes():
    """The scan dies with its shell. Without this file, "how far along is the
    run in that other window?" has no answer at all."""
    # Uppercase deliberately. The blob includes the cache and status PATHS,
    # and tempfile builds its directory name from [a-z0-9_] - so a 2-char
    # lowercase id like "d4" lands inside a random temp path about 2% of the
    # time and fails this assertion for no reason. Uppercase cannot collide.
    ids = ["MSGA", "MSGB", "MSGC", "MSGD"]
    with tempfile.TemporaryDirectory() as d:
        a = _fetch_args(d, ids, status=os.path.join(d, "fetch-status.json"))
        _run_fetch(a, _FakeTransport(ids))

        st = g.read_status(a.status)
        assert st, "no status file was written"
        # The terminal state: a reader after the fact must see an outcome,
        # not a run that looks stalled forever.
        assert st["state"] == "done", st
        assert st["command"] == "fetch"
        assert st["done"] == 4 and st["total"] == 4 and st["dropped"] == 0
        assert st["query"] == "in:inbox"
        assert st["stale"] is False
        # No addresses and no message IDs: the query is the only mailbox-shaped
        # value in here, and it is why the file is gitignored.
        blob = json.dumps(st)
        assert "@" not in blob, blob
        for i in ids:
            assert i not in blob, i


def test_status_file_reports_a_killed_scan_as_stale():
    """A killed scan leaves state 'running' behind forever. mtime is what
    separates "still going" from "the process is gone"."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "fetch-status.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"command": "fetch", "state": "running", "done": 120,
                       "total": 300, "rate": 22.3, "eta": 8.0}, f)
        assert g.read_status(p)["stale"] is False, "fresh file is not stale"

        old = time.time() - (g.STATUS_STALE_AFTER + 30)
        os.utime(p, (old, old))
        st = g.read_status(p)
        assert st["stale"] is True, st
        # A rate from a process that no longer exists reads as live throughput.
        line = " ".join(g._status_lines(st))
        assert "STALE" in line and "22.3" not in line, line
        assert "resume" in line, line


def test_status_never_probes_the_pid():
    """os.kill(pid, 0) is the usual liveness idiom and a trap here: on Windows
    os.kill ignores the signal and calls TerminateProcess, so the line that
    asks whether the scan is alive would kill it.

    Over the parsed tree, not the text: the comment that explains the trap
    names the call, and banning the string would ban the explanation.
    """
    tree = ast.parse(open(SOURCE, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        assert name not in ("kill", "terminate"), ast.dump(fn)


def test_status_writing_never_fails_a_scan():
    """Telemetry. A scan that dies because its status file was unwritable
    would be a worse bug than having no status file."""
    ids = ["a1", "b2", "c3"]
    with tempfile.TemporaryDirectory() as d:
        # A directory where the file should be: every write raises.
        blocked = os.path.join(d, "blocked")
        os.mkdir(blocked)
        a = _fetch_args(d, ids, status=blocked)
        _run_fetch(a, _FakeTransport(ids))
        assert len(g.load_cache(a.cache)) == 3, "the scan must still finish"


def test_status_and_review_files_are_gitignored():
    """They hold sender addresses and mailbox queries - the same class of data
    as headers.jsonl."""
    for name in (g.FETCH_STATUS, g.ENGAGED_STATUS, g.REVIEW_FILE,
                 g.REVIEW_FILE + ".tmp", g.FETCH_STATUS + ".tmp"):
        assert _gitignored(name), name


def test_ui_reports_a_scan_started_in_another_terminal():
    """The CLI is the reference path, so a scan is at least as likely to have
    been started from a terminal as from the page."""
    orig_progress = g.PROGRESS
    try:
        g.PROGRESS = None
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "fetch-status.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"command": "fetch", "state": "running", "done": 900,
                           "total": 35012, "dropped": 2, "rate": 27.6,
                           "avg_rate": 24.0, "eta": 1400.0,
                           "query": "in:inbox"}, f)
            args = argparse.Namespace(sanitize=None, cache="headers.jsonl",
                                      batch=1000, dropped="", status=p)
            with _ui_server(args) as (_, port, token):
                code, _, body = _ui_req(port, "GET", "/api/progress",
                                        token=token)
        d2 = json.loads(body)
    finally:
        g.PROGRESS = orig_progress
    assert code == 200, code
    assert d2["scan"]["source"] == "file", d2["scan"]
    assert d2["scan"]["phase"] == "running", d2["scan"]
    assert d2["progress"]["done"] == 900 and d2["progress"]["total"] == 35012


# ------------------------------------------------------------------ review
def _review_rows(engaged=()):
    return g.rank_rows(g.load_cache(FIXTURE), engaged)


def _marks(path):
    marks, errors = g.parse_review(path, strict=True)
    assert not errors, errors
    return marks


def test_review_file_starts_completely_unmarked():
    """The friction being removed is partly protective. A file that arrives
    pre-marked and needs only a save is more dangerous than typing the
    addresses out."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        summary = g.write_review(p, _review_rows())
        assert summary["marked"] == 0, summary
        assert set(_marks(p).values()) == {False}


def test_preselect_never_marks_a_safeguarded_sender():
    """--preselect-score is the bulk select. It must not be able to sweep a
    protected sender along, at any threshold."""
    rows = _review_rows(engaged={"newsletter@vendor.example.org"})
    guarded = {r["sender"] for r in rows if r["guard"]}
    assert guarded, "the fixture must contain safeguarded senders"
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        g.write_review(p, rows, preselect_score=1)  # mark everything it can
        marks = _marks(p)
        for sender in guarded:
            assert marks[sender] is False, sender
        assert any(marks.values()), "it should still mark the unguarded ones"


def test_review_never_truncates_a_long_sender():
    """A clipped address either fails validation and makes the whole file
    unreadable, or still looks like an address and silently names a sender
    that does not exist. Real mail is full of addresses past 43 characters;
    the example.com fixture has none, which is how this shipped and why a
    real 5,192-sender file had 62 unparseable rows."""
    long_sender = "account-security-noreply-department@accountprotection.example.com"
    assert len(long_sender) > 44
    rows = [
        {"sender": long_sender, "count": 12, "score": 8, "signals": ["no-reply"],
         "rec": "Trash", "guard": None},
        {"sender": "short@example.com", "count": 3, "score": 0, "signals": [],
         "rec": "Keep", "guard": "starred"},
    ]
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        g.write_review(p, rows)
        marks, errors = g.parse_review(p, strict=True)
        text = open(p, encoding="utf-8").read()
    assert not errors, errors
    assert set(marks) == {long_sender, "short@example.com"}, marks
    assert long_sender in text, "the full address must survive into the file"
    # ...and the columns still line up behind the longest one.
    body = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert len({l.index(" 12 ".strip()) for l in body[:1]}) == 1


def test_min_score_writes_only_the_rows_the_tool_has_an_opinion_about():
    """5,192 rows is an archive, not a work list. On the first real mailbox,
    86% of the review pile was score 3-5, where the scorer simply does not
    know; --min-score 6 leaves the senders recommended for Trash plus the ones
    a safeguard actually held back."""
    rows = _review_rows()
    expected = {r["sender"] for r in rows if r["score"] >= 6}
    assert 0 < len(expected) < len(rows), "the fixture must exercise both sides"
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        summary = g.write_review(p, rows, min_score=6)
        marks = _marks(p)
        text = open(p, encoding="utf-8").read()
    assert set(marks) == expected, set(marks) ^ expected
    assert summary["hidden"] == len(rows) - len(expected), summary
    assert summary["dropped"] == 0, "hidden is not dropped"
    assert "FILTERED" in text, "a truncated file must say it is truncated"


def test_min_score_never_hides_a_sender_you_already_marked():
    """The rule that makes the filter a view rather than a rewrite. Without
    it, narrowing the file silently discards decisions already made, and the
    file is the only record of them."""
    rows = _review_rows()
    low = min(rows, key=lambda r: r["score"])
    assert low["score"] < 6, low
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        g.write_review(p, rows)                      # full file
        lines = open(p, encoding="utf-8").read().splitlines()
        with open(p, "w", encoding="utf-8") as f:    # decide one low-scoring row
            for l in lines:
                f.write(("t" + l[1:] if low["sender"] in l else l) + "\n")
        assert _marks(p)[low["sender"]] is True

        summary = g.write_review(p, rows, min_score=6)   # now narrow it
        marks = _marks(p)
    assert marks[low["sender"]] is True, "a decided row must survive the filter"
    assert summary["kept_marked"] == 1, summary


def test_a_filtered_file_widens_again_with_every_mark_intact():
    """Round trip. Moving between the narrow view and the full file must cost
    nothing, or the filter is a trap rather than a convenience."""
    rows = _review_rows()
    chosen = "news@deals.example.com"
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        g.write_review(p, rows, min_score=6)
        lines = open(p, encoding="utf-8").read().splitlines()
        with open(p, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(("t" + l[1:] if chosen in l else l) + "\n")

        g.write_review(p, rows)                      # widen back out
        marks = _marks(p)
        text = open(p, encoding="utf-8").read()
    assert set(marks) == {r["sender"] for r in rows}, "every sender is back"
    assert marks[chosen] is True, "the decision survived the round trip"
    assert "FILTERED" not in text, "an unfiltered file must not claim to be one"


def test_min_score_cannot_preselect_a_row_it_hides():
    """Filter first, then preselect. Marking a sender the operator was never
    shown is exactly the pre-marked file the review format exists to avoid."""
    rows = _review_rows()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        summary = g.write_review(p, rows, min_score=6, preselect_score=1)
        marks = _marks(p)
    assert all(r >= 6 for r in
               [row["score"] for row in rows if row["sender"] in marks])
    assert summary["preselected"] == len([m for m in marks.values() if m])


def test_review_and_senders_file_produce_the_same_targets():
    """DESIGN-UI.md's phase 3 done-when, one layer down: selection through the
    new artifact must equal the set the old one would act on."""
    chosen = ["news@deals.example.com", "no-reply@sketchy.example.net"]
    with tempfile.TemporaryDirectory() as d:
        review = os.path.join(d, "review.txt")
        g.write_review(review, _review_rows())
        lines = open(review, encoding="utf-8").read().splitlines()
        with open(review, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(("t" + l[1:] if any(c in l for c in chosen) else l) + "\n")

        senders = os.path.join(d, "approved.txt")
        with open(senders, "w", encoding="utf-8") as f:
            f.write("\n".join(chosen) + "\n")

        via_review = _dry_run_targets(d, review=review)
        via_senders = _dry_run_targets(d, senders=senders)
    assert via_review == via_senders, (len(via_review), len(via_senders))
    assert via_review, "the fixture should match something"


def _dry_run_targets(d, review=None, senders=None):
    """Run cmd_trash as a dry run and return the manifest's message IDs."""
    manifest = os.path.join(d, "m-{}.jsonl".format("r" if review else "s"))
    a = argparse.Namespace(
        sanitize=None, review=review, senders=senders or "approved.txt",
        engaged="", allow_missing_engaged=True,
        cache=FIXTURE, manifest=manifest, batch=250,
        concurrency=2, execute=False, yes=True,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        g.cmd_trash(a)
    return {json.loads(l)["id"] for l in open(manifest, encoding="utf-8")
            if l.strip()}


def test_review_regeneration_carries_marks_forward():
    """Review in several sittings, or fetch more mail part way through, and
    the decisions already made must survive. Silently discarding a
    half-finished review is worse than anything overwriting would prevent."""
    rows = _review_rows()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        g.write_review(p, rows)
        lines = open(p, encoding="utf-8").read().splitlines()
        with open(p, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(("t" + l[1:] if "news@deals" in l else l) + "\n")

        # Re-rank over a cache that has grown by one sender.
        extra = dict(rows[0], sender="brand-new@example.com", count=1,
                     score=0, signals=[], guard=None, rec="Keep")
        summary = g.write_review(p, rows + [extra])
        marks = _marks(p)
    assert marks["news@deals.example.com"] is True, "the mark was lost"
    assert marks["brand-new@example.com"] is False, "new senders start unmarked"
    assert summary["carried"] == len(rows) and summary["kept_marked"] == 1


def test_review_parse_errors_name_the_line():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# header\n"
                    "t     news@deals.example.com\n"
                    "t     news@deals\n"                  # not an address
                    "X     other@example.com\n"           # unknown mark
                    "t     news@deals.example.com\n")     # duplicate
        marks, errors = g.parse_review(p, strict=True)
    assert len(errors) == 3, errors
    joined = " | ".join(errors)
    assert "line 3" in joined and "line 4" in joined and "line 5" in joined
    assert "is not an address" in joined and "already appears" in joined


def test_a_typo_that_is_still_a_valid_address_is_refused_not_skipped():
    """The failure mode the format exists to remove. A transposed domain is
    syntactically valid, so no parser can catch it; in approved.txt it simply
    matches nothing and quietly does less than asked. The review file is
    generated from the cache, which is what makes "matches nothing" a
    detectable contradiction rather than a plausible line."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("t   news@deals.example.com\n"
                    "t   news@deals.exmaple.com\n")   # transposed, valid
        marks, errors = g.parse_review(p, strict=True)
        assert not errors, "syntactically fine - the parser cannot help here"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                _dry_run_targets(d, review=p)
        except SystemExit as e:
            assert "exmaple" in str(e) and "typo" in str(e), e
            return
    raise AssertionError("a marked sender matching nothing must stop the run")


def test_review_parser_tolerates_hand_editing():
    """Split on whitespace, so re-aligning or reordering rows by hand is fine
    and only the mark and the address carry meaning."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("t news@deals.example.com\n"
                    "\n"
                    ".\t\t[!]   alerts@mybank.example.com   30  8  whatever\n"
                    "t   [!] promo@shop.example.com\n")
        marks, errors = g.parse_review(p, strict=True)
    assert not errors, errors
    assert marks == {"news@deals.example.com": True,
                     "alerts@mybank.example.com": False,
                     "promo@shop.example.com": True}


def test_a_mark_in_the_wrong_column_says_so():
    """The commonest way to get this file wrong: leave the '.' where it is and
    add a 't' beside it. "'t' is not an address" is true and useless - the
    reader needs telling where the mark goes."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(".     t     news@deals.example.com\n"        # beside it
                    ".  {}  t  promo@shop.example.com\n".format(   # past the flag
                        g.REVIEW_GUARD_FLAG)
                    + "t           ok@example.com\n")              # correct
        marks, errors = g.parse_review(p, strict=True)
    assert marks == {"ok@example.com": True}, marks
    assert len(errors) == 2, errors
    for e in errors:
        assert "column 1" in e, e
        assert "is not an address" not in e, e


def test_a_genuinely_mangled_address_still_reads_as_one():
    """The clearer message must not swallow the case it was carved out of."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("t     not-an-address\n")
        _, errors = g.parse_review(p, strict=True)
    assert len(errors) == 1 and "is not an address" in errors[0], errors


def test_trash_recomputes_the_safeguard_rather_than_trusting_the_file():
    """Deleting the [!] flag by hand removes the marker, not the warning."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("t   alerts@mybank.example.com\n")   # no [!] anywhere
        out = io.StringIO()
        a = argparse.Namespace(
            sanitize=None, review=p, senders="approved.txt", engaged="",
            allow_missing_engaged=True,
            cache=FIXTURE, manifest=os.path.join(d, "m.jsonl"), batch=250,
            concurrency=2, execute=False, yes=True,
        )
        with contextlib.redirect_stdout(out):
            g.cmd_trash(a)
    text = out.getvalue()
    assert "SAFEGUARD OVERRIDE" in text, text
    assert "protected-domain" in text, text


def test_trash_honours_the_important_guard_mode():
    """cmd_trash recomputes the guard, so it needs the same --important-guard
    the ranking used. The default is the cautious end: a run that ranked with
    `off` and trashes without the flag gets MORE warnings, not fewer."""
    sender = "updates@service.example.com"  # IMPORTANT on 9 of 12

    def override_block(mode):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "review.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("t   {}\n".format(sender))
            out = io.StringIO()
            a = argparse.Namespace(
                sanitize=None, review=p, senders="approved.txt", engaged="",
                allow_missing_engaged=True, important_guard=mode,
                cache=FIXTURE, manifest=os.path.join(d, "m.jsonl"), batch=250,
                concurrency=2, execute=False, yes=True,
            )
            with contextlib.redirect_stdout(out):
                g.cmd_trash(a)
            return out.getvalue()

    for mode in ("majority", "any"):
        text = override_block(mode)
        assert "SAFEGUARD OVERRIDE" in text and "important" in text, (mode, text)
    text = override_block("off")
    assert "SAFEGUARD OVERRIDE" not in text, text
    # ...and the approval is still obeyed either way: safeguards demote the
    # ranking, they never veto a sender the human listed.
    assert "Matching messages: 12" in text, text


def test_review_header_records_the_important_mode():
    """The mode changes which senders arrive flagged, so a file that does not
    say which one produced it cannot be read a week later."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        g.write_review(p, _review_rows(), important="off")
        head = open(p, encoding="utf-8").read()
    assert "IMPORTANT guard: off" in head, head


RELABELS = {}   # msg_id -> labels the stubbed _relabel_one was asked to add


def _run_mutation(fn, args, fail_ids=(), error="429 rate limit",
                  relabel_error=None):
    """Run a mutating command with some message IDs made to fail.

    Returns (stdout, exit_message or None). The default error is a throttle,
    which is deliberately RETRYABLE: the circuit breaker counts consecutive
    non-retryable failures only, so a test that wants it to fire has to say so.
    """
    def flaky(msg_id, sanitize=None):
        if msg_id in fail_ids:
            raise RuntimeError(error)
        return msg_id

    orig_t, orig_u, orig_r = g._trash_one, g._untrash_one, g._relabel_one
    g._trash_one = g._untrash_one = flaky
    def relabel(mid, labels, sanitize=None):
        if relabel_error:
            raise RuntimeError(relabel_error)
        RELABELS[mid] = list(labels)
        return mid

    g._relabel_one = relabel
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                fn(args)
            except SystemExit as e:
                return out.getvalue(), str(e)
        return out.getvalue(), None
    finally:
        g._trash_one, g._untrash_one = orig_t, orig_u
        g._relabel_one = orig_r


def _trash_args(d, **kw):
    review = os.path.join(d, "review.txt")
    with open(review, "w", encoding="utf-8") as f:
        f.write("t   news@deals.example.com\n")
    ns = dict(sanitize=None, review=review, senders="approved.txt", engaged="",
              allow_missing_engaged=True, cache=FIXTURE,
              manifest=os.path.join(d, "m.jsonl"), batch=250, concurrency=2,
              execute=True, yes=True)
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_a_failed_trash_is_not_counted_as_a_success():
    """_safe_mutate swallows the error so one bad message cannot abandon the
    batch, which means the loop body runs either way. Counting iterations
    rather than return values reported every attempt as a success: a run in
    which every single call failed still printed "60 messages moved to Trash".
    That is the one number a person uses to decide whether the mutation
    worked, and it could only ever be wrong in the unsafe direction."""
    msgs = [m for m in g.load_cache(FIXTURE)
            if g.addr_of(m["headers"].get("from", "")) == "news@deals.example.com"]
    doomed = {m["id"] for m in msgs[:4]}
    assert len(doomed) == 4 and len(msgs) > 4
    with tempfile.TemporaryDirectory() as d:
        out, exit_msg = _run_mutation(g.cmd_trash, _trash_args(d), doomed)
    assert "{} messages moved to Trash".format(len(msgs) - 4) in out, out[-400:]
    assert exit_msg is not None, "a partial failure must not exit 0"
    assert "4 of {} messages FAILED".format(len(msgs)) in exit_msg, exit_msg
    assert "retry with" in exit_msg and "--execute" in exit_msg, exit_msg


def test_a_clean_trash_run_says_so_and_exits_zero():
    msgs = [m for m in g.load_cache(FIXTURE)
            if g.addr_of(m["headers"].get("from", "")) == "news@deals.example.com"]
    with tempfile.TemporaryDirectory() as d:
        out, exit_msg = _run_mutation(g.cmd_trash, _trash_args(d))
    assert exit_msg is None, exit_msg
    assert "{} messages moved to Trash".format(len(msgs)) in out, out[-400:]
    assert "FAILED" not in out


def _restore_from(rows, fail_ids=(), relabel_error=None, cache=""):
    """Run untrash over a manifest built from `rows`."""
    RELABELS.clear()
    with tempfile.TemporaryDirectory() as d, _in(d):
        manifest = os.path.join(d, "m.jsonl")
        with open(manifest, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        a = argparse.Namespace(sanitize=None, manifest=manifest, concurrency=1,
                               execute=True, status="", cache=cache)
        out, exit_msg = _run_mutation(g.cmd_untrash, a, set(fail_ids),
                                      relabel_error=relabel_error)
    return out, exit_msg, dict(RELABELS)


def test_a_restore_puts_the_inbox_label_back():
    """messages.untrash clears TRASH and does not restore INBOX: a real
    1,108-message restore reported success and landed every message in All
    Mail. An undo that cannot put things back is not an undo."""
    rows = [
        {"id": "A", "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"]},
        {"id": "B", "labelIds": ["INBOX", "UNREAD", "IMPORTANT"]},
        {"id": "C", "labelIds": ["CATEGORY_PROMOTIONS"]},   # was archived
    ]
    out, exit_msg, relabelled = _restore_from(rows)
    assert exit_msg is None, exit_msg
    assert set(relabelled) == {"A", "B"}, relabelled
    assert relabelled["A"] == ["INBOX"] and relabelled["B"] == ["INBOX"], (
        "only INBOX is restored: the others survive the round trip, and "
        "re-adding UNREAD would resurrect a read state from cache time")
    assert "2 of them will be put back in the inbox" in out, out


def test_a_manifest_without_labels_says_where_the_mail_will_land():
    """Manifests written before label recording cannot say where a message
    belonged. Saying so is the difference between a known limitation and a
    person hunting through All Mail wondering what went wrong."""
    out, exit_msg, relabelled = _restore_from(
        [{"id": "A", "sender": "x@example.com"}, {"id": "B"}], cache="")
    assert exit_msg is None, exit_msg
    assert not relabelled, relabelled
    assert "2 have no labels" in out, out
    assert "All Mail" in out, out


def test_untrash_recovers_missing_labels_from_the_header_cache():
    """A manifest written before label recording has no idea where its
    messages belonged - but the cache those messages were selected from does,
    and recorded them at fetch time. Reading that back is recovery, not a
    guess."""
    msgs = g.load_cache(FIXTURE)[:3]
    rows = [{"id": m["id"], "sender": "x@example.com"} for m in msgs]
    rows.append({"id": "NOT-IN-CACHE"})
    out, exit_msg, relabelled = _restore_from(rows, cache=FIXTURE)
    assert exit_msg is None, exit_msg
    assert set(relabelled) == {m["id"] for m in msgs}, relabelled
    assert all(v == ["INBOX"] for v in relabelled.values()), relabelled
    assert "3 had no labels recorded; filled in from" in out, out
    assert "3 of them will be put back in the inbox" in out, out
    # ...and the one the cache cannot answer for is still reported honestly.
    assert "1 have no labels" in out, out


def test_a_current_manifest_never_reads_the_cache():
    """The manifest answers for every row, so the cache is not consulted -
    which matters because a cache rebuilt since the trash describes the
    mailbox now, not the mailbox then."""
    called = []
    orig = g.load_cache
    g.load_cache = lambda p: called.append(p) or orig(p)
    try:
        out, exit_msg, relabelled = _restore_from(
            [{"id": "A", "labelIds": ["INBOX"]}], cache=FIXTURE)
    finally:
        g.load_cache = orig
    assert not called, "a labelled manifest must not touch the cache"
    assert relabelled == {"A": ["INBOX"]}, relabelled


def test_a_half_restored_message_is_not_a_success():
    """Untrash then relabel is one unit of work. A message that left Trash but
    never got its INBOX back is half restored, and counting it as a success
    would be the same lie as counting attempts."""
    out, exit_msg, _ = _restore_from([{"id": "A", "labelIds": ["INBOX"]}],
                                     relabel_error="500 backendError")
    assert "Restored 0 messages" in out, out
    assert exit_msg is not None and "1 of 1 messages FAILED" in exit_msg, exit_msg


def test_a_failed_untrash_is_not_counted_as_a_success():
    """The undo path has the same counter and the same consequence: a restore
    that silently did nothing would be discovered only in Gmail."""
    with tempfile.TemporaryDirectory() as d:
        manifest = os.path.join(d, "m.jsonl")
        ids = ["MSG{}".format(i) for i in range(10)]
        with open(manifest, "w", encoding="utf-8") as f:
            for i in ids:
                f.write(json.dumps({"id": i, "sender": "x@example.com",
                                    "date": "", "subject": ""}) + "\n")
        a = argparse.Namespace(sanitize=None, manifest=manifest,
                               concurrency=2, execute=True)
        out, exit_msg = _run_mutation(g.cmd_untrash, a, set(ids[:3]))
    assert "Restored 7 messages from Trash." in out, out[-300:]
    assert exit_msg is not None and "3 of 10 messages FAILED" in exit_msg, exit_msg


@contextlib.contextmanager
def _in(d):
    """Run in d, and come back. Windows cannot delete a directory that is a
    process's cwd, so the restore has to happen before the tempdir is."""
    orig = os.getcwd()
    try:
        os.chdir(d)
        yield
    finally:
        os.chdir(orig)


def test_the_manifest_records_what_labels_a_message_had():
    """messages.untrash clears TRASH and does NOT restore INBOX: measured on a
    real 1,108-message restore, which came back to All Mail rather than to the
    inbox. The undo cannot put a message back without knowing where it was,
    and the cache already holds that, so recording it costs no API call."""
    with tempfile.TemporaryDirectory() as d, _in(d):
        args = _trash_args(d, manifest=os.path.join(d, "m.jsonl"), execute=False)
        _run_mutation(g.cmd_trash, args)
        rows = [json.loads(l) for l in
                open(args.manifest, encoding="utf-8") if l.strip()]
    assert rows, "the fixture sender must produce targets"
    for r in rows:
        assert "labelIds" in r, r
        assert "INBOX" in r["labelIds"], r
    # ...and the manifest is still written before anything moves, so a dry run
    # produces exactly the same record.
    assert len({tuple(sorted(r["labelIds"])) for r in rows}) >= 1


def test_a_second_trash_run_does_not_overwrite_the_first_undo_list():
    """cmd_trash opened one fixed path with 'w', so trashing a second batch
    destroyed the first batch's manifest and the only warning was a person
    remembering to copy the file. The undo list is the recovery path for an
    operation that moves real mail; it must not be the thing that quietly goes
    missing."""
    with tempfile.TemporaryDirectory() as d, _in(d):
        first = _run_mutation(g.cmd_trash, _trash_args(d, manifest=None))[0]
        second = _run_mutation(g.cmd_trash, _trash_args(d, manifest=None))[0]
        written = sorted(f for f in os.listdir(".")
                         if f.startswith(g.MANIFEST_PREFIX))
        sizes = [len(open(f, encoding="utf-8").read().splitlines())
                 for f in written]
    assert len(written) == 2, written
    assert sizes[0] == sizes[1] > 0, sizes
    for out in (first, second):
        assert "Manifest written" in out, out[:200]


def test_an_explicit_manifest_is_honoured_exactly():
    """The default is per-run, but a named path is a decision, not a hint."""
    with tempfile.TemporaryDirectory() as d, _in(d):
        named = os.path.join(d, "keep-this.jsonl")
        _run_mutation(g.cmd_trash, _trash_args(d, manifest=named))
        assert os.path.exists(named)
        assert not [f for f in os.listdir(".")
                    if f.startswith(g.MANIFEST_PREFIX)]


def test_manifest_names_never_collide():
    """Two runs in the same second would be silent, which is the combination
    worth guarding."""
    with tempfile.TemporaryDirectory() as d, _in(d):
        when = datetime.datetime(2026, 9, 9, 11, 30, 45)
        seen = []
        for _ in range(3):
            p = g.manifest_path(when)
            open(p, "w", encoding="utf-8").close()
            seen.append(p)
    assert len(set(seen)) == 3, seen


def test_untrash_picks_the_most_recent_run_and_says_which():
    """Restoring the wrong run is the failure this command exists to prevent,
    so the choice is printed - and a dry run is the default, so there is a
    chance to read it before anything moves."""
    with tempfile.TemporaryDirectory() as d, _in(d):
        old = g.manifest_path(datetime.datetime(2026, 9, 1, 9, 0, 0))
        with open(old, "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "OLD1"}) + "\n")
        new = g.manifest_path(datetime.datetime(2026, 9, 9, 9, 0, 0))
        with open(new, "w", encoding="utf-8") as f:
            for i in range(4):
                f.write(json.dumps({"id": "NEW{}".format(i)}) + "\n")
        os.utime(old, (1000, 1000))          # unambiguously older
        os.utime(new, (2000, 2000))
        a = argparse.Namespace(sanitize=None, manifest=None, concurrency=2,
                               execute=False)
        out, exit_msg = _run_mutation(g.cmd_untrash, a)
    assert exit_msg is None, exit_msg
    assert os.path.basename(new) in out, out
    assert "1 other present" in out, out
    assert "Restoring 4 messages" in out, out


def test_untrash_with_nothing_to_restore_says_so():
    with tempfile.TemporaryDirectory() as d, _in(d):
        a = argparse.Namespace(sanitize=None, manifest=None, concurrency=2,
                               execute=False)
        out, exit_msg = _run_mutation(g.cmd_untrash, a)
    assert exit_msg is not None and "no manifest found" in exit_msg, exit_msg
    assert "--manifest" in exit_msg, exit_msg


def test_a_mutation_publishes_its_progress_like_a_scan():
    """Every long-running command here reported progress except the two that
    move mail, which is backwards: a scan you cannot see is an annoyance, a
    mutation you cannot see is the one you most want to watch. A 1,108-message
    restore printed one line and then nothing for four minutes."""
    with tempfile.TemporaryDirectory() as d, _in(d):
        manifest = os.path.join(d, "m.jsonl")
        ids = ["MSG{}".format(i) for i in range(12)]
        with open(manifest, "w", encoding="utf-8") as f:
            for i in ids:
                f.write(json.dumps({"id": i}) + "\n")
        status = os.path.join(d, "untrash-status.json")
        a = argparse.Namespace(sanitize=None, manifest=manifest, concurrency=2,
                               execute=True, status=status)
        _run_mutation(g.cmd_untrash, a, set(ids[:2]))
        published = g.read_status(status)
    assert published, "the mutation must publish a status file"
    assert published["command"] == "untrash", published
    assert published["state"] == "done", published
    assert published["done"] == 10 and published["dropped"] == 2, published
    assert published["total"] == 12, published


def test_a_mutation_publishes_progress_while_it_is_still_running():
    """The final write is the easy half and proves nothing: a 1,108-message
    restore that printed one line, went silent for four minutes and then
    published a finished status file would pass that. What was missing is the
    reporter ticking DURING the run, which is the whole point of asking."""
    orig_rep, orig_one = g._progress_reporter, g._untrash_one

    def fast(progress, limiter, stop, interval=2.0, plain_every=10.0,
             status=None):
        # The real reporter, just ticking faster than a test can wait for.
        return orig_rep(progress, limiter, stop, interval=0.02,
                        plain_every=0.02, status=status)

    def slow(msg_id, sanitize=None):
        time.sleep(0.03)
        return msg_id

    with tempfile.TemporaryDirectory() as d, _in(d):
        manifest = os.path.join(d, "m.jsonl")
        with open(manifest, "w", encoding="utf-8") as f:
            for i in range(60):
                f.write(json.dumps({"id": "MSG{}".format(i)}) + "\n")
        status = os.path.join(d, "untrash-status.json")
        a = argparse.Namespace(sanitize=None, manifest=manifest, concurrency=2,
                               execute=True, status=status)
        mid = []
        g._progress_reporter, g._untrash_one = fast, slow
        try:
            def run():
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    try:
                        g.cmd_untrash(a)
                    except SystemExit:
                        pass

            t = threading.Thread(target=run)
            t.start()

            def caught_mid_run():
                d2 = g.read_status(status)
                if d2 and d2["state"] == "running" and 0 < d2["done"] < 60:
                    mid.append(d2)
                    return True
                return False

            _wait_for(caught_mid_run, timeout=15)
            t.join(timeout=15)
            assert not t.is_alive(), "the restore never finished"
            final = g.read_status(status)
        finally:
            g._progress_reporter, g._untrash_one = orig_rep, orig_one
    assert mid, "no status was published while the run was in flight"
    assert mid[0]["command"] == "untrash" and mid[0]["total"] == 60, mid[0]
    assert final["state"] == "done" and final["done"] == 60, final


def test_status_reads_a_mutation_without_being_told_where():
    """`status` with no arguments has to cover the mutating commands too, or
    the file they publish is one nobody looks at."""
    for name in (g.TRASH_STATUS, g.UNTRASH_STATUS):
        assert name.endswith("-status.json"), name
        assert _gitignored(name), name
    a = argparse.Namespace(sanitize=None, files=[], json=False)
    src = inspect.getsource(g.cmd_status)
    for const in ("TRASH_STATUS", "UNTRASH_STATUS"):
        assert const in src, "status must look for {} by default".format(const)


def _untrash_all(n, error):
    """Run untrash over n ids, every one of them failing with `error`."""
    with tempfile.TemporaryDirectory() as d, _in(d):
        manifest = os.path.join(d, "m.jsonl")
        ids = ["MSG{}".format(i) for i in range(n)]
        with open(manifest, "w", encoding="utf-8") as f:
            for i in ids:
                f.write(json.dumps({"id": i}) + "\n")
        a = argparse.Namespace(sanitize=None, manifest=manifest, concurrency=1,
                               execute=True, status="")
        return _run_mutation(g.cmd_untrash, a, set(ids), error=error)


def test_a_mutation_stops_after_a_wall_of_systemic_failures():
    """25 consecutive NON-RETRYABLE failures means something systemic - an
    expired refresh token, a revoked scope - and hammering the remaining
    thousand is waste. The manifest still names every target, so the run
    stays resumable."""
    out, exit_msg = _untrash_all(60, "invalid_grant: token has been expired")
    assert exit_msg is not None, out
    assert "STOPPED" in exit_msg and "consecutive" in exit_msg, exit_msg
    assert "Retry with" in exit_msg, exit_msg
    # It stopped rather than running the whole list.
    assert "of 60 messages" not in exit_msg, exit_msg


def test_throttles_never_trip_the_breaker():
    """A throttle means the fleet is too fast, not that the run is doomed.
    Counting it toward the breaker would abort long runs on a healthy
    mailbox, which is the opposite of what the breaker is for."""
    out, exit_msg = _untrash_all(60, "429 rate limit")
    assert exit_msg is not None, out           # they all failed, so non-zero
    assert "STOPPED" not in exit_msg, exit_msg  # ...but not by giving up
    assert "60 of 60 messages FAILED" in exit_msg, exit_msg


def test_rank_rows_is_the_single_ranking():
    """The table, the JSON and the review file are three renderings of one
    ranking, not three rankings kept in agreement by hand."""
    rows = _review_rows()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "review.txt")
        g.write_review(p, rows)
        marks = _marks(p)
    assert set(marks) == {r["sender"] for r in rows}
    # ...and the file lists them in the ranking's own order, so the rows that
    # most want a decision are the ones at the top.
    with tempfile.TemporaryDirectory() as d2:
        p2 = os.path.join(d2, "review.txt")
        g.write_review(p2, rows)
        listed = [l.split()[-0 or 0] and l.split() for l in
                  open(p2, encoding="utf-8").read().splitlines()
                  if l and not l.startswith("#")]
    senders = [parts[1] if parts[1] != g.REVIEW_GUARD_FLAG else parts[2]
               for parts in listed]
    assert senders == [r["sender"] for r in rows], senders


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


class _KeepOpen(io.BytesIO):
    """The handler closes its rfile in finish(); the test still needs to read
    what was left in it."""

    def close(self):
        pass


class _FakeSocket:
    """Just enough socket for BaseHTTPRequestHandler, with an observable rfile.

    A real loopback socket cannot show this: on Linux the client gets its 403
    whether or not the body was read, because the bytes were already delivered
    before the close. The whole failure is that Windows sends RST instead of
    FIN when a socket is closed with unread bytes still buffered. So the test
    asks the portable question directly - did the server consume the body -
    rather than the platform-specific one.
    """

    def __init__(self, raw):
        self.rfile = _KeepOpen(raw)
        self.sent = bytearray()

    def makefile(self, mode="rb", *a, **k):
        return self.rfile if "r" in mode else io.BytesIO()

    def sendall(self, b):
        self.sent.extend(b)

    def settimeout(self, *a):
        pass

    def shutdown(self, *a):
        pass

    def close(self):
        pass


def _handle_raw(build, token="test-token-value"):
    """Run one request through _UIHandler against a fake socket.

    `build(port)` returns the raw bytes; it takes the port because the Host
    allowlist is built from the server's real address, and a request that gets
    Host wrong is refused for that reason instead of the one under test.
    """
    httpd = g.make_ui_server(port=0, token=token,
                             args=argparse.Namespace(sanitize=None,
                                                     cache="headers.jsonl",
                                                     batch=1000, dropped=""))
    try:
        sock = _FakeSocket(build(httpd.server_address[1]))
        g._UIHandler(sock, ("127.0.0.1", 12345), httpd)
        return sock, bytes(sock.sent)
    finally:
        httpd.server_close()


def _raw_post(port, body, extra=""):
    return ("POST /api/scan HTTP/1.0\r\n"
            "Host: 127.0.0.1:{}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {}\r\n"
            "{}\r\n".format(port, len(body), extra)).encode() + body


def test_ui_reads_the_request_body_even_when_it_refuses_the_request():
    """Windows resets a connection closed with bytes still unread in its
    receive buffer, so the client sees WinError 10053 instead of the 403 that
    was actually sent. Every rejection path replies without looking at the
    body, which is exactly when it bites: a POST to /api/scan with a bad token
    or a foreign Origin carries JSON nobody reads, and the page shows a network
    error in place of the reason it was refused."""
    body = json.dumps({"query": "in:inbox", "concurrency": 4}).encode()
    for extra in ("Origin: http://evil.example\r\nX-Audit-Token: test-token-value\r\n",
                  "X-Audit-Token: wrong\r\n"):
        sock, sent = _handle_raw(lambda port, e=extra: _raw_post(port, body, e))
        assert b"403" in sent.split(b"\r\n")[0], (extra, sent[:80])
        assert b"bad Origin" in sent or b"bad token" in sent, sent[-120:]
        assert sock.rfile.read() == b"", (
            "the refused request's body must be consumed before replying, or "
            "closing the socket resets the connection and destroys the 403")


def test_ui_does_not_double_read_a_body_it_already_parsed():
    """The drain is skipped once _read_json has taken the body. Reading it
    twice would block on bytes that are never coming."""
    body = json.dumps({"query": "in:inbox", "concurrency": 99}).encode()
    sock, sent = _handle_raw(
        lambda port: _raw_post(port, body,
                               "X-Audit-Token: test-token-value\r\n"))
    # 99 is over the drop threshold, so this is refused on its merits - by the
    # handler that DID read the body, which is the path being covered.
    assert b"400" in sent.split(b"\r\n")[0], sent[:80]
    assert sock.rfile.read() == b""


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


def test_ui_refuses_to_start_a_scan_over_one_running_elsewhere():
    """It shares this mailbox's quota and this cache. The button is disabled
    for it, but the guard has to be on the server: the page is not the only
    thing that can post to that endpoint."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "fetch-status.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"command": "fetch", "state": "running", "done": 900,
                       "total": 35012, "pid": 4242}, f)
        args = argparse.Namespace(sanitize=None, cache="headers.jsonl",
                                  batch=1000, dropped="", status=p)
        with _ui_server(args) as (httpd, port, token):
            code, _, body = _ui_req(port, "POST", "/api/scan", token=token,
                                    body={"concurrency": 4})
            assert code == 409, code
            assert "4242" in json.loads(body)["error"], body
            assert httpd.ui_scan.snapshot()["status"] == "idle"

            # ...but a scan that died does not block one forever.
            old = time.time() - (g.STATUS_STALE_AFTER + 30)
            os.utime(p, (old, old))
            code, _, body = _ui_req(port, "GET", "/api/progress", token=token)
            assert json.loads(body)["scan"]["phase"] == "stale", body
            assert json.loads(body)["limiter"] is None, "a dead run has no pace"

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
    orig_cwd = os.getcwd()
    err = io.StringIO()
    try:
        g._run, g.LIMITER = _FakeTransport(ids), None
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                sanitize=None, cache=os.path.join(d, "headers.jsonl"),
                batch=1000, dropped=os.path.join(d, "dropped.jsonl"),
            )
            try:
                # Run from a scratch cwd. The status assertion below is about
                # a RELATIVE path, so from the project directory it reads the
                # fetch-status.json a real scan left there and fails for a
                # reason that has nothing to do with the code. It passed in CI
                # and on a fresh clone, and failed the first time it met a
                # checkout the tool had actually been used in.
                os.chdir(d)
                with contextlib.redirect_stderr(err):
                    with _ui_server(args) as (httpd, port, token):
                        code, _, _ = _ui_req(
                            port, "POST", "/api/scan", token=token,
                            body={"query": "in:inbox", "concurrency": 4,
                                  "limit": 0},
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
                # A namespace with no --status means disabled. Now that the
                # cwd is scratch, this genuinely tests that: a default guessed
                # in cmd_fetch would land right here.
                assert not os.path.exists(g.FETCH_STATUS), (
                    "no --status means disabled - cmd_fetch must not guess a "
                    "default and write into the working directory")
            finally:
                # Before the tempdir is removed: on Windows a directory cannot
                # be deleted while it is some process's cwd.
                os.chdir(orig_cwd)
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


# -------------------------------------------------- engaged resumability
class _FakeSent(object):
    """A sent-mail transport. Each message has one distinct recipient, so the
    union of addresses says exactly which messages were scanned."""

    def __init__(self, ids, fail=()):
        self.ids = list(ids)
        self.fail = set(fail)
        self.requested = []

    def __call__(self, cmd):
        argv = list(cmd)
        if "list" in argv:
            return _Proc(0, json.dumps(
                {"messages": [{"id": i} for i in self.ids]}) + "\n")
        if "get" in argv:
            params = json.loads(argv[argv.index("--params") + 1])
            mid = params["id"]
            self.requested.append(mid)
            if mid in self.fail:
                return _Proc(1, "", "requested entity was not found")
            return _Proc(0, json.dumps({
                "id": mid,
                "internalDate": "1700000000000",
                "labelIds": ["SENT"],
                "payload": {"headers": [
                    {"name": "To", "value": "{}@example.com".format(mid)},
                ]},
            }))
        return _Proc(1, "", "unexpected argv")


def _engaged_args(d, **kw):
    a = argparse.Namespace(
        sanitize=None, out=os.path.join(d, "engaged.txt"),
        cache=os.path.join(d, "engaged-cache.jsonl"),
        concurrency=4, limit=0,
        dropped=os.path.join(d, "engaged-dropped.jsonl"), status="",
    )
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _run_engaged(a, transport):
    orig_run, orig_limiter = g._run, g.LIMITER
    err = io.StringIO()
    try:
        g._run, g.LIMITER = transport, None
        with contextlib.redirect_stderr(err):
            g.cmd_engaged(a)
    finally:
        g._run, g.LIMITER = orig_run, orig_limiter
    return err.getvalue()


def test_engaged_checkpoints_every_message_it_scans():
    ids = ["m{}".format(i) for i in range(6)]
    with tempfile.TemporaryDirectory() as d:
        a = _engaged_args(d)
        _run_engaged(a, _FakeSent(ids))
        rows = [json.loads(l) for l in open(a.cache, encoding="utf-8")
                if l.strip()]
    assert {r["id"] for r in rows} == set(ids)
    # Only the id and the extracted addresses - no headers, no subject.
    assert all(set(r) == {"id", "addrs"} for r in rows), rows[0]


def test_engaged_rerun_only_fetches_what_is_missing():
    """The whole point: a scan that dies at minute 18 of 20 should cost
    minutes to finish, not start over. Two real runs were lost before this."""
    ids = ["m{}".format(i) for i in range(10)]
    missing = {"m3", "m7"}
    with tempfile.TemporaryDirectory() as d:
        a = _engaged_args(d)
        # First pass drops two. A dropped ID is never checkpointed.
        first = _FakeSent(ids, fail=missing)
        _run_engaged(a, first)
        assert len(first.requested) >= len(ids)

        # Second pass, clean transport: only the two gaps are re-requested.
        second = _FakeSent(ids)
        _run_engaged(a, second)
        assert set(second.requested) == missing, second.requested

        addrs = {l.strip() for l in open(a.out, encoding="utf-8") if l.strip()}
    assert addrs == {"{}@example.com".format(i) for i in ids}, addrs


def test_engaged_writes_no_partial_safeguard_list_when_it_aborts():
    """require_engaged() only checks that engaged.txt EXISTS, so a partial
    list would pass the guard while covering a fraction of the people you
    write to. The checkpoint survives an aborted run; the artifact does not."""
    ids = ["m{}".format(i) for i in range(60)]
    with tempfile.TemporaryDirectory() as d:
        a = _engaged_args(d)
        transport = _FakeSent(ids, fail=set(ids))
        transport.__class__.__call__.__doc__ = None
        try:
            _run_engaged(a, _FakeSentAuthExpired(ids))
        except SystemExit as e:
            assert "gws auth login" in str(e), e
            assert not os.path.exists(a.out), "no partial engaged.txt"
            return
    raise AssertionError("an aborted engaged scan must not write the list")


class _FakeSentAuthExpired(_FakeSent):
    """Every get fails the way an expired refresh token does - which matches
    neither retry regex, so the circuit breaker trips."""

    def __call__(self, cmd):
        if "get" in list(cmd):
            return _Proc(1, "", "invalid_grant: token expired")
        return _FakeSent.__call__(self, cmd)


def test_engaged_cache_is_jsonl_and_gitignored():
    assert g.ENGAGED_CACHE.endswith(".jsonl"), g.ENGAGED_CACHE
    assert _gitignored(g.ENGAGED_CACHE), g.ENGAGED_CACHE


def test_load_engaged_cache_unions_addresses_across_records():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "c.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "a", "addrs": ["x@example.com"]}) + "\n")
            f.write(json.dumps({"id": "b", "addrs": []}) + "\n")
            f.write("not json\n")
            f.write(json.dumps({"id": "c", "addrs": ["x@example.com",
                                                     "y@example.com"]}) + "\n")
        ids, addrs = g.load_engaged_cache(p)
    # A message with no recipients still counts as scanned, or it would be
    # re-fetched forever.
    assert ids == {"a", "b", "c"}
    assert addrs == {"x@example.com", "y@example.com"}
    assert g.load_engaged_cache(os.path.join(d, "nope.jsonl")) == (set(), set())



# ------------------------------------------------- the engaged-list guard
def _rank_args(d, **kw):
    a = argparse.Namespace(
        cache=FIXTURE, engaged=os.path.join(d, "engaged.txt"), json=False,
        review=None, preselect_score=0, allow_missing_engaged=False,
    )
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _run_rank(a):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        g.cmd_rank(a)
    return out.getvalue(), err.getvalue()


def test_rank_review_refuses_without_the_engaged_list():
    """A warning is not enough where a trash list is born. cmd_engaged already
    refuses to write an empty list for the same reason: a silently inert
    safeguard is worse than a loud failure, because the output looks correct
    either way."""
    with tempfile.TemporaryDirectory() as d:
        a = _rank_args(d, review=os.path.join(d, "review.txt"))
        try:
            _run_rank(a)
        except SystemExit as e:
            assert "INACTIVE" in str(e) and "engaged" in str(e), e
            assert "--allow-missing-engaged" in str(e), e
            assert not os.path.exists(a.review), "nothing should be written"
            return
    raise AssertionError("rank --review must refuse with no engaged list")


def test_rank_table_still_only_warns():
    """The table is informational. Only the review file becomes a trash list,
    so only it refuses."""
    with tempfile.TemporaryDirectory() as d:
        out, err = _run_rank(_rank_args(d))
    assert "INACTIVE" in err, err
    assert "news@deals.example.com" in out, "the table still prints"


def test_rank_review_proceeds_with_the_explicit_override():
    with tempfile.TemporaryDirectory() as d:
        a = _rank_args(d, review=os.path.join(d, "review.txt"),
                       allow_missing_engaged=True)
        _run_rank(a)
        assert os.path.exists(a.review)


def test_an_empty_engaged_file_is_an_answer_not_a_gap():
    """A mailbox with no sent mail is a real state. Only a MISSING file means
    the step was never run."""
    with tempfile.TemporaryDirectory() as d:
        a = _rank_args(d, review=os.path.join(d, "review.txt"))
        open(a.engaged, "w").close()
        _run_rank(a)
        assert os.path.exists(a.review)


def test_trash_refuses_without_the_engaged_list():
    """Checked at the step that moves mail too, not only at rank time: a
    review file can reach trash written on another machine, or from before
    the list existed. Without it the SAFEGUARD OVERRIDE block under-reports -
    it still sees protected domains and stars, so it prints a confident,
    incomplete answer."""
    with tempfile.TemporaryDirectory() as d:
        review = os.path.join(d, "review.txt")
        with open(review, "w", encoding="utf-8") as f:
            f.write("t   news@deals.example.com\n")
        a = argparse.Namespace(
            sanitize=None, review=review, senders="approved.txt",
            engaged=os.path.join(d, "engaged.txt"),
            allow_missing_engaged=False, cache=FIXTURE,
            manifest=os.path.join(d, "m.jsonl"), batch=250, concurrency=2,
            execute=False, yes=True,
        )
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                g.cmd_trash(a)
        except SystemExit as e:
            assert "INACTIVE" in str(e), e
            assert not os.path.exists(a.manifest), "no manifest before the guard"
            return
    raise AssertionError("trash must refuse with no engaged list")


# ------------------------------------------------------------------ doctor
def _doctor(mode, engaged="/nonexistent"):
    """Run cmd_doctor over a stubbed transport. Returns (stdout, exit_code)."""
    profile = {"emailAddress": "someone@example.com", "messagesTotal": 35012,
               "threadsTotal": 21004, "historyId": "998877"}
    errors = {
        "scope": "Request had insufficient authentication scopes. [403]",
        "noauth": "Access denied. No credentials provided.",
    }
    orig_run, orig_gws, orig_lim = g._run, g.GWS, g.LIMITER
    out, code = io.StringIO(), 0
    try:
        g._run = _FakeProfile(profile if mode == "ok" else None,
                              errors.get(mode))
        g.GWS = sys.executable      # a real path, so the gws row passes
        g.LIMITER = None
        try:
            with contextlib.redirect_stdout(out):
                g.cmd_doctor(argparse.Namespace(sanitize=None, engaged=engaged))
        except SystemExit as e:
            code = 1 if e.code else 0
            if isinstance(e.code, str):
                out.write(e.code)
    finally:
        g._run, g.GWS, g.LIMITER = orig_run, orig_gws, orig_lim
    return out.getvalue(), code


def test_doctor_reports_ready_and_names_the_next_command():
    text, code = _doctor("ok")
    assert code == 0, text
    assert "Ready" in text and "35,012" in text
    # engaged comes first for the reason require_engaged enforces
    assert "gmail_audit.py engaged" in text, text


def test_doctor_distinguishes_the_two_auth_failures():
    """The whole point: learn which of them you have from a command that
    costs nothing, not from a scan that dies twenty minutes in."""
    scope, scope_code = _doctor("scope")
    noauth, noauth_code = _doctor("noauth")
    assert scope_code == 1 and noauth_code == 1
    assert "no Gmail scope" in scope, scope
    assert "not authenticated" in noauth, noauth
    # the literal error text stays verbatim, because SETUP.md indexes by it
    assert "insufficient authentication scopes" in scope, scope
    # and the fix stays copy-pasteable rather than wrapped mid-URL
    assert "auth/gmail.modify,openid," in noauth, noauth


def test_doctor_exits_nonzero_when_gws_is_absent():
    orig_gws = g.GWS
    try:
        g.GWS = "definitely-not-a-real-binary-xyz"
        out, code = io.StringIO(), 0
        try:
            with contextlib.redirect_stdout(out):
                g.cmd_doctor(argparse.Namespace(sanitize=None, engaged="x"))
        except SystemExit as e:
            code = 1 if e.code else 0
    finally:
        g.GWS = orig_gws
    assert code == 1
    assert "not on PATH" in out.getvalue(), out.getvalue()


def test_preflight_labels_do_not_drift():
    """One label table. The page carries a JS copy it cannot import, so the
    two are asserted to cover the same statuses instead."""
    statuses = {s for s, _ in g.UI_ERRORS} | {"ok", "error"}
    assert set(g.PREFLIGHT_LABELS) == statuses, g.PREFLIGHT_LABELS
    for status in statuses:
        assert status + ":" in g.UI_HTML, "UI_HTML PF_LABEL is missing " + status



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
        except (Exception, SystemExit) as e:
            # SystemExit is a BaseException, so a bare `except Exception`
            # lets an unexpected sys.exit() inside a test abort the whole run
            # and report nothing. That should be one failure, not silence.
            failed += 1
            print("  FAIL  {}: {}".format(name, str(e).splitlines()[:1]))
    print("\n{}/{} passed".format(len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
