#!/usr/bin/env python3
"""Check analyze.py against the synthetic fixture from gen_fixture.py.

    python3 latency-trace/tools/testdata/test_analyze.py
"""

import csv
import importlib.util
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


analyze = load_module("analyze", os.path.join(HERE, "..", "analyze.py"))
gen = load_module("gen_fixture", os.path.join(HERE, "gen_fixture.py"))


def ms(stats, label):
    """{n, p50, max} of one stage, latencies rounded to microseconds."""
    for lbl, s in stats:
        if lbl == label:
            if s["n"] == 0:
                return {"n": 0}
            return {"n": s["n"], "p50": round(s["p50"] / 1e6, 3),
                    "max": round(s["max"] / 1e6, 3)}
    raise KeyError(label)


class FixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        gen.main(cls.tmp.name)
        d = cls.tmp.name
        cls.paths = [os.path.join(d, f)
                     for f in ("trace.txt", "inject.txt", "evdev.txt")]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_keys(self):
        idx, mode, chains, unmatched, excluded = analyze.run(self.paths)
        self.assertEqual(excluded, 0)
        self.assertEqual(mode, "keys")
        self.assertEqual(idx.pres_clocks, {1})
        st = analyze.compute_stats(chains, mode)
        expect = {
            "origin -> wl_key": (4, 0.35, 0.5),
            "wl_key -> pty_write": (4, 0.2, 0.2),
            "pty_write -> pty_read (app echo)": (4, 1.0, 2.0),
            "pty_read -> parse_apply": (4, 0.1, 0.1),
            "parse_apply -> gui_output": (4, 0.15, 0.3),
            "gui_output -> paint_start": (4, 0.15, 5.1),
            "paint_start -> paint_end": (4, 1.5, 2.0),
            "paint_end -> commit": (4, 0.15, 0.3),
            "commit -> presented": (3, 6.4, 9.6),
            "TOTAL origin -> presented": (3, 10.0, 20.0),
            "wl_key -> presented": (3, 9.7, 19.6),
        }
        for label, (n, p50, mx) in expect.items():
            self.assertEqual(ms(st, label), {"n": n, "p50": p50, "max": mx},
                             label)
        status = sorted(c["status"] for c in chains)
        self.assertEqual(status, ["discarded", "no_pty_write", "presented",
                                  "presented", "presented"])
        self.assertEqual(sum(c["deferred"] for c in chains), 1)
        self.assertEqual(unmatched, 1)
        kinds = sorted(c["origin_kind"] for c in chains)
        self.assertEqual(kinds, ["evdev_key", "inject_key", "inject_key",
                                 "inject_key", "wl_key"])
        # chain 2 skipped the stale paint and waited for the frame callback
        c2 = chains[1]
        self.assertEqual((c2["seqno"], c2["frame_id"], c2["deferred"]),
                         (11, 102, 1))

    def test_output(self):
        _, mode, chains, _, excluded = analyze.run(self.paths, mode="output")
        self.assertEqual(mode, "output")
        self.assertEqual(len(chains), 4)
        # the startup read before the first commit
        self.assertEqual(excluded, 1)
        # the second burst read is ambiguous and left out of the statistics
        self.assertEqual(sum(c["ambiguous"] for c in chains), 1)
        st = analyze.compute_stats(chains, mode)
        self.assertEqual(ms(st, "TOTAL pty_read -> presented"),
                         {"n": 3, "p50": 8.0, "max": 8.0})
        self.assertEqual(ms(st, "commit -> presented"),
                         {"n": 3, "p50": 6.4, "max": 6.6})
        # the late chain's parse_apply and gui_output come after its paint
        # started; those stages are reported as they are, negative included
        self.assertEqual(ms(st, "parse_apply -> gui_output"),
                         {"n": 3, "p50": 0.1, "max": 0.9})
        self.assertEqual(ms(st, "gui_output -> paint_start"),
                         {"n": 3, "p50": 0.1, "max": 0.1})
        _, _, all_chains, _, all_excluded = analyze.run(
            self.paths, mode="output", echo_window_ms=0)
        self.assertEqual(len(all_chains), 8)
        self.assertEqual(all_excluded, 1)
        self.assertEqual(sorted(c["status"] for c in all_chains),
                         ["discarded"] + ["presented"] * 7)

    def test_parse_apply_after_paint(self):
        # parse_apply recorded inside the paint that shows its seqno: the
        # chain belongs to that paint (frame 107), not the repaint (108).
        _, _, chains, _, _ = analyze.run(self.paths, mode="output")
        late = [c for c in chains if c["pty_read"] == gen.us(500_000)]
        self.assertEqual(len(late), 1)
        c = late[0]
        self.assertEqual((c["seqno"], c["frame_id"], c["status"]),
                         (15, 107, "presented"))
        self.assertEqual(c["paint_start"], gen.us(500_100))
        self.assertEqual(c["paint_end"], gen.us(501_000))
        self.assertEqual(c["parse_apply"], gen.us(500_300))
        self.assertEqual(c["gui_output"], gen.us(501_200))
        self.assertEqual(c["presented"] - c["pty_read"], 7_000_000)

    def test_ambiguous_when_parser_behind(self):
        # The second burst read arrives before any parse_apply for the first,
        # so its first parse_apply (seqno 16) may cover only the first read;
        # here it does, and the chain wrongly lands on frame 109. It must be
        # flagged and kept out of the statistics; the first read is fine.
        _, _, chains, _, _ = analyze.run(self.paths, mode="output")
        first = [c for c in chains if c["pty_read"] == gen.us(600_000)]
        second = [c for c in chains if c["pty_read"] == gen.us(600_100)]
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual((first[0]["ambiguous"], first[0]["seqno"],
                          first[0]["frame_id"]), (0, 16, 109))
        self.assertEqual(second[0]["ambiguous"], 1)
        self.assertEqual(second[0]["seqno"], 16)
        # every other chain is unambiguous, including the late one whose
        # parse_apply comes after its paint started
        self.assertEqual([c["pty_read"] for c in chains if c["ambiguous"]],
                         [gen.us(600_100)])
        # including the ambiguous chain would change the statistics
        st_all = analyze.stage_stats(chains, "pty_read", "presented")
        st = analyze.compute_stats(chains, "output")
        self.assertEqual(st_all["n"], 4)
        self.assertEqual(ms(st, "TOTAL pty_read -> presented")["n"], 3)

    def test_cli_output_reports_ambiguous(self):
        out_csv = os.path.join(self.tmp.name, "out_output.csv")
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = analyze.main(self.paths[:1] + ["--mode", "output",
                                                "--csv", out_csv])
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertIn("ambiguous chains (parser behind, excluded from "
                      "stats): 1", buf.getvalue())
        with open(out_csv) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["ambiguous"] for r in rows],
                         ["0", "0", "0", "1"])

    def test_cli_output_reports_excluded(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = analyze.main(self.paths[:1] + ["--mode", "output"])
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertIn("excluded pty_reads before the first commit "
                      "(startup): 1", buf.getvalue())

    def test_wl_time_wrap(self):
        # wl_time_ms is a u32; CLOCK_MONOTONIC ms may exceed 2**32.
        origin_ns = (2 ** 32 + 5) * 1_000_000 + 400_000
        self.assertEqual(analyze.wl_ms_delta(5, origin_ns), 0)
        self.assertEqual(analyze.wl_ms_delta(6, origin_ns), 1)
        self.assertEqual(analyze.wl_ms_delta(4, origin_ns), -1)
        self.assertEqual(analyze.wl_ms_delta(2 ** 32 - 1, 0), -1)

    def test_cli_and_csv(self):
        out_csv = os.path.join(self.tmp.name, "out.csv")
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = analyze.main(self.paths + ["--csv", out_csv])
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertIn("TOTAL origin -> presented", buf.getvalue())
        with open(out_csv) as f:
            rows = f.read().splitlines()
        self.assertEqual(len(rows), 6)
        self.assertTrue(rows[0].startswith("chain,origin_kind,"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
