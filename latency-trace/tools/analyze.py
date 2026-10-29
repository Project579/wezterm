#!/usr/bin/env python3
"""Analyze a wezterm latency trace (see latency-trace/README.md).

Usage:
    analyze.py TRACE [EXTRA ...] [--mode keys|output] [--csv OUT.csv]

TRACE is the file written by wezterm when WEZTERM_LATENCY_TRACE is set.
EXTRA files (output of sniff_evdev.py or inject_keys.py) use the same
`<ts_ns>,<event>,<values...>` format and are merged by timestamp.

Modes:
  keys    (default when the trace has wl_key presses) one chain per key
          press: origin (inject_key / evdev_key, else wl_key) -> wl_key ->
          pty_write -> pty_read (echo) -> parse_apply -> gui_output ->
          paint_start -> paint_end -> commit -> presented.
  output  one chain per pty_read (reads preceded by a pty_write on the same
          pane within --echo-window-ms are skipped, and reads before the
          first commit, i.e. startup output, are excluded and counted)
          through the same downstream stages.

A chain is marked ambiguous when the previous pty_read on its pane has no
parse_apply between it and the chain's own pty_read: the parser was behind,
so the first parse_apply after the read may cover only the earlier read and
its seqno may lead to a paint without this read's content. Ambiguous chains
are excluded from the stage statistics, counted in the report, and flagged
in the CSV `ambiguous` column.

All timestamps are CLOCK_MONOTONIC nanoseconds. `presented` is measured at
the presented_ns value from wp_presentation (start of scanout), not at the
time the feedback event was received.
"""

import argparse
import bisect
import csv
import sys
from collections import Counter, defaultdict

CLOCK_MONOTONIC = 1
WL_TIME_WRAP = 1 << 32


class Event:
    __slots__ = ("ts", "name", "vals")

    def __init__(self, ts, name, vals):
        self.ts = ts
        self.name = name
        self.vals = vals

    def __repr__(self):
        return "Event(%d,%s,%r)" % (self.ts, self.name, self.vals)


def load(paths):
    """Parse and merge trace files; returns events sorted by timestamp."""
    events = []
    for path in paths:
        with open(path) as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                try:
                    ts = int(parts[0])
                    vals = [int(v) for v in parts[2:]]
                except (ValueError, IndexError):
                    print("warning: %s:%d: unparsable line ignored: %r"
                          % (path, lineno, line), file=sys.stderr)
                    continue
                events.append(Event(ts, parts[1], vals))
    # Records come from several threads, so the file is only roughly ordered.
    events.sort(key=lambda e: e.ts)
    return events


class Series:
    """Events of one kind (optionally one pane), sorted, with bisect helpers."""

    def __init__(self, events=None):
        self.events = events or []
        self.ts = [e.ts for e in self.events]

    def first_at_or_after(self, ts, start=0):
        i = bisect.bisect_left(self.ts, ts, lo=start)
        return i if i < len(self.ts) else None

    def last_at_or_before(self, ts):
        i = bisect.bisect_right(self.ts, ts) - 1
        return i if i >= 0 else None

    def first_after(self, ts):
        i = bisect.bisect_right(self.ts, ts)
        return i if i < len(self.ts) else None

    def any_between(self, lo, hi, pred=lambda e: True):
        i = bisect.bisect_left(self.ts, lo)
        while i < len(self.ts) and self.ts[i] <= hi:
            if pred(self.events[i]):
                return True
            i += 1
        return False

    def __len__(self):
        return len(self.events)


class Index:
    def __init__(self, events):
        by_name = defaultdict(list)
        by_pane = defaultdict(list)
        self.presented = {}
        self.discarded = set()
        self.pres_clocks = set()
        for e in events:
            by_name[e.name].append(e)
            if e.name in ("pty_write", "pty_read", "parse_apply",
                          "gui_output", "paint_pane") and e.vals:
                by_pane[(e.name, e.vals[0])].append(e)
            elif e.name == "presented" and len(e.vals) >= 2:
                self.presented[e.vals[0]] = e
            elif e.name == "discarded" and e.vals:
                self.discarded.add(e.vals[0])
            elif e.name == "pres_clock" and e.vals:
                self.pres_clocks.add(e.vals[0])
        self.by_name = {k: Series(v) for k, v in by_name.items()}
        self.by_pane = {k: Series(v) for k, v in by_pane.items()}

    def series(self, name, pane=None):
        if pane is None:
            return self.by_name.get(name) or Series()
        return self.by_pane.get((name, pane)) or Series()


# Chain fields, in pipeline order. Each holds a CLOCK_MONOTONIC ns value or
# None when the chain did not reach that stage.
TS_FIELDS = ["origin", "wl_key", "pty_write", "pty_read", "parse_apply",
             "gui_output", "paint_start", "paint_end", "commit", "presented"]


def new_chain(origin_kind=None, origin=None):
    c = {f: None for f in TS_FIELDS}
    c.update(origin_kind=origin_kind, origin=origin, pane=None, seqno=None,
             frame_id=None, deferred=0, ambiguous=0, status=None)
    return c


def follow_downstream(idx, c):
    """Fill in the stages after pty_read for chain `c` (pane and pty_read set).

    Sets c["status"] to "presented", "discarded" or "no_<stage>", and
    c["ambiguous"] to 1 when the parser may still have been behind.
    """
    pane = c["pane"]
    s = idx.series("parse_apply", pane)

    # The reader and parser are separate threads and the parser coalesces
    # queued reads, so the first parse_apply after this pty_read may belong
    # to an earlier read that was still queued; its seqno is then too low and
    # can match a paint that does not contain this read. That happened if
    # the previous read on this pane has no parse_apply between it and this
    # one (checking the previous read covers all earlier ones: a parse_apply
    # after it is after them too).
    reads = idx.series("pty_read", pane)
    j = reads.first_at_or_after(c["pty_read"])
    if j is not None and j > 0:
        prev = reads.ts[j - 1]
        if not s.any_between(prev, c["pty_read"] - 1):
            c["ambiguous"] = 1

    i = s.first_at_or_after(c["pty_read"])
    if i is None:
        c["status"] = "no_parse_apply"
        return
    c["parse_apply"] = s.ts[i]
    c["seqno"] = seqno = s.events[i].vals[1]

    # parse_apply's timestamp is taken after perform_actions has applied the
    # parsed actions and released the terminal lock. If the parser thread is
    # preempted in between, a paint may already have taken the lock and shown
    # the new content, so parse_apply can land after gui_output, paint_pane
    # or even the whole paint. Search the later stages from pty_read instead;
    # the stage latencies relative to parse_apply are then reported as they
    # are, negative values included.
    s = idx.series("gui_output", pane)
    i = s.first_at_or_after(c["pty_read"])
    if i is not None:
        c["gui_output"] = s.ts[i]

    # First paint of this pane that reflects the parsed content: a paint_pane
    # seqno is a lower bound on the content painted, so seqno >= S proves the
    # paint shows it, wherever parse_apply's timestamp landed.
    s = idx.series("paint_pane", pane)
    i = s.first_at_or_after(c["pty_read"])
    while i is not None and i < len(s) and s.events[i].vals[1] < seqno:
        i += 1
    if i is None or i >= len(s):
        c["status"] = "no_paint"
        return
    paint_pane_ts = s.ts[i]

    starts = idx.series("paint_start")
    ends = idx.series("paint_end")
    j = starts.last_at_or_before(paint_pane_ts)
    k = ends.first_at_or_after(paint_pane_ts)
    if j is None or k is None:
        c["status"] = "no_paint"
        return
    c["paint_start"] = starts.ts[j]
    c["paint_end"] = ends.ts[k]

    if c["gui_output"] is not None:
        c["deferred"] = int(idx.series("invalidate").any_between(
            c["gui_output"], c["paint_start"],
            lambda e: len(e.vals) >= 2 and e.vals[1] == 1))

    # The GUI thread paints and commits sequentially, so the commit belongs
    # to this paint only if it comes before the next paint_start.
    commits = idx.series("commit")
    m = commits.first_at_or_after(c["paint_end"])
    nxt = starts.first_after(c["paint_end"])
    if m is None or (nxt is not None and commits.ts[m] > starts.ts[nxt]):
        c["status"] = "no_commit"
        return
    c["commit"] = commits.ts[m]
    c["frame_id"] = fid = commits.events[m].vals[1]

    if fid == 0:
        c["status"] = "no_feedback"
    elif fid in idx.presented:
        c["presented"] = idx.presented[fid].vals[1]
        c["status"] = "presented"
    elif fid in idx.discarded:
        c["status"] = "discarded"
    else:
        c["status"] = "no_presented"


def wl_ms_delta(wl_ms, origin_ns):
    """wl_time_ms - floor(origin_ns / 1e6), allowing for u32 wraparound."""
    d = (wl_ms - origin_ns // 1_000_000) % WL_TIME_WRAP
    return d - WL_TIME_WRAP if d >= WL_TIME_WRAP // 2 else d


def analyze_keys(idx):
    """Returns (chains, unmatched_origins)."""
    wl = [e for e in idx.series("wl_key").events
          if len(e.vals) >= 2 and e.vals[1] == 1]
    origins = [e for e in idx.series("inject_key").events]
    origins += [e for e in idx.series("evdev_key").events
                if e.vals and e.vals[-1] == 1]
    origins.sort(key=lambda e: e.ts)

    # Match each origin to the unclaimed wl_key press whose wl_time_ms is
    # within +-1 of floor(origin_ns/1e6), nearest first.
    origin_of = {}
    unmatched_origins = 0
    wl_ts = [e.ts for e in wl]
    for o in origins:
        lo = bisect.bisect_left(wl_ts, o.ts)
        best = None
        for wi in range(lo, len(wl)):
            w = wl[wi]
            if w.ts - o.ts > 1_000_000_000:
                break
            if wi in origin_of:
                continue
            d = abs(wl_ms_delta(w.vals[0], o.ts))
            if d <= 1 and (best is None or d < best[0]):
                best = (d, wi)
                if d == 0:
                    break
        if best is None:
            unmatched_origins += 1
        else:
            origin_of[best[1]] = o

    writes = idx.series("pty_write")
    chains = []
    for wi, w in enumerate(wl):
        o = origin_of.get(wi)
        if o is not None:
            c = new_chain(o.name, o.ts)
        else:
            c = new_chain("wl_key", w.ts)
        c["wl_key"] = w.ts
        chains.append(c)

        # The bytes for this key are written before the next key press;
        # presses that write nothing (modifiers) end here.
        next_wl = wl[wi + 1].ts if wi + 1 < len(wl) else None
        i = writes.first_at_or_after(w.ts)
        if i is None or (next_wl is not None and writes.ts[i] >= next_wl):
            c["status"] = "no_pty_write"
            continue
        c["pty_write"] = writes.ts[i]
        c["pane"] = pane = writes.events[i].vals[0]

        reads = idx.series("pty_read", pane)
        i = reads.first_at_or_after(c["pty_write"])
        if i is None:
            c["status"] = "no_pty_read"
            continue
        c["pty_read"] = reads.ts[i]
        follow_downstream(idx, c)
    return chains, unmatched_origins


def analyze_output(idx, echo_window_ns):
    """Returns (chains, excluded_startup).

    Reads before the first commit in the trace are output produced before
    the window was first shown; their chains measure startup, not the
    output path, so they are counted in excluded_startup and dropped.
    """
    chains = []
    excluded_startup = 0
    commits = idx.series("commit")
    first_commit = commits.ts[0] if len(commits) else None
    for r in idx.series("pty_read").events:
        pane = r.vals[0]
        if echo_window_ns > 0:
            ws = idx.series("pty_write", pane)
            j = ws.last_at_or_before(r.ts)
            if j is not None and r.ts - ws.ts[j] <= echo_window_ns:
                continue
        if first_commit is not None and r.ts < first_commit:
            excluded_startup += 1
            continue
        c = new_chain("pty_read", r.ts)
        c["pane"] = pane
        c["pty_read"] = r.ts
        chains.append(c)
        follow_downstream(idx, c)
    return chains, excluded_startup


def percentile(sorted_vals, q):
    """Linear-interpolated percentile of an ascending list, q in [0, 1]."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def stage_stats(chains, a, b):
    """Latencies (ns) from field a to field b over chains that have both."""
    vals = sorted(c[b] - c[a] for c in chains
                  if c[a] is not None and c[b] is not None)
    if not vals:
        return {"n": 0}
    return {"n": len(vals),
            "p50": percentile(vals, 0.50),
            "p95": percentile(vals, 0.95),
            "p99": percentile(vals, 0.99),
            "max": vals[-1]}


KEY_STAGES = [
    ("origin", "wl_key", "origin -> wl_key"),
    ("wl_key", "pty_write", "wl_key -> pty_write"),
    ("pty_write", "pty_read", "pty_write -> pty_read (app echo)"),
    ("pty_read", "parse_apply", "pty_read -> parse_apply"),
    ("parse_apply", "gui_output", "parse_apply -> gui_output"),
    ("gui_output", "paint_start", "gui_output -> paint_start"),
    ("paint_start", "paint_end", "paint_start -> paint_end"),
    ("paint_end", "commit", "paint_end -> commit"),
    ("commit", "presented", "commit -> presented"),
    ("origin", "presented", "TOTAL origin -> presented"),
    ("wl_key", "presented", "wl_key -> presented"),
]
OUTPUT_STAGES = KEY_STAGES[3:9] + [
    ("pty_read", "presented", "TOTAL pty_read -> presented"),
]


def compute_stats(chains, mode):
    """Per-stage statistics over the chains that are not ambiguous."""
    stages = KEY_STAGES if mode == "keys" else OUTPUT_STAGES
    chains = [c for c in chains if not c["ambiguous"]]
    out = []
    for a, b, label in stages:
        if mode == "keys" and a == "origin" and b == "wl_key":
            # Only chains with a real origin (inject/evdev) count here.
            sub = [c for c in chains if c["origin_kind"] != "wl_key"]
        else:
            sub = chains
        out.append((label, stage_stats(sub, a, b)))
    return out


def fmt_ms(ns):
    return "%.3f" % (ns / 1e6)


def print_report(chains, mode, stats, unmatched_origins, idx, out=None,
                 excluded_startup=0):
    out = out or sys.stdout
    print("mode: %s, chains: %d" % (mode, len(chains)), file=out)
    print("%-36s %6s %9s %9s %9s %9s" % ("stage (ms)", "n", "p50", "p95",
                                          "p99", "max"), file=out)
    for label, s in stats:
        if s["n"] == 0:
            print("%-36s %6d %9s %9s %9s %9s" % (label, 0, "-", "-", "-", "-"),
                  file=out)
        else:
            print("%-36s %6d %9s %9s %9s %9s" % (
                label, s["n"], fmt_ms(s["p50"]), fmt_ms(s["p95"]),
                fmt_ms(s["p99"]), fmt_ms(s["max"])), file=out)
    status = Counter(c["status"] for c in chains)
    deferred = sum(c["deferred"] for c in chains)
    unmatched = len(chains) - status["presented"] - status["discarded"]
    print("deferred paints (waited for frame callback): %d" % deferred,
          file=out)
    print("discarded frames: %d" % status["discarded"], file=out)
    print("ambiguous chains (parser behind, excluded from stats): %d"
          % sum(c["ambiguous"] for c in chains), file=out)
    print("unmatched chains: %d %s" % (unmatched, dict(sorted(
        (k, v) for k, v in status.items()
        if k not in ("presented", "discarded")))), file=out)
    if mode == "keys":
        print("chains by origin: %s" % dict(sorted(Counter(
            c["origin_kind"] for c in chains).items())), file=out)
        print("origins without a matching wl_key: %d" % unmatched_origins,
              file=out)
    else:
        print("excluded pty_reads before the first commit (startup): %d"
              % excluded_startup, file=out)


CSV_FIELDS = ["chain", "origin_kind", "pane", "seqno", "frame_id",
              "deferred", "ambiguous", "status"] + [f + "_ns" for f in TS_FIELDS]


def write_csv(path, chains):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for n, c in enumerate(chains):
            row = [n, c["origin_kind"], c["pane"], c["seqno"], c["frame_id"],
                   c["deferred"], c["ambiguous"], c["status"]]
            row += [c[fld] for fld in TS_FIELDS]
            w.writerow(["" if v is None else v for v in row])


def run(paths, mode=None, echo_window_ms=50.0):
    events = load(paths)
    idx = Index(events)
    if mode is None:
        has_press = any(len(e.vals) >= 2 and e.vals[1] == 1
                        for e in idx.series("wl_key").events)
        mode = "keys" if has_press else "output"
    unmatched_origins = 0
    excluded_startup = 0
    if mode == "keys":
        chains, unmatched_origins = analyze_keys(idx)
    else:
        chains, excluded_startup = analyze_output(
            idx, int(echo_window_ms * 1e6))
    return idx, mode, chains, unmatched_origins, excluded_startup


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Per-stage latency report for a wezterm latency trace.")
    p.add_argument("trace", help="trace written via WEZTERM_LATENCY_TRACE")
    p.add_argument("extra", nargs="*",
                   help="extra files (sniff_evdev.py / inject_keys.py output)")
    p.add_argument("--mode", choices=["keys", "output"],
                   help="default: keys if the trace has wl_key presses")
    p.add_argument("--echo-window-ms", type=float, default=50.0,
                   help="output mode: skip pty_reads preceded by a pty_write "
                        "on the same pane within this many ms (0: keep all)")
    p.add_argument("--csv", metavar="OUT", help="write per-chain timestamps")
    args = p.parse_args(argv)

    idx, mode, chains, unmatched_origins, excluded_startup = run(
        [args.trace] + args.extra, args.mode, args.echo_window_ms)

    if not idx.pres_clocks:
        print("note: no pres_clock record; assuming presented_ns is "
              "CLOCK_MONOTONIC", file=sys.stderr)
    elif idx.pres_clocks != {CLOCK_MONOTONIC}:
        print("WARNING: presentation clock is %s, not CLOCK_MONOTONIC (1); "
              "commit -> presented and totals are not comparable"
              % sorted(idx.pres_clocks), file=sys.stderr)

    print_report(chains, mode, compute_stats(chains, mode),
                 unmatched_origins, idx, excluded_startup=excluded_startup)
    if args.csv:
        write_csv(args.csv, chains)
        print("wrote %d chains to %s" % (len(chains), args.csv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
