#!/usr/bin/env python3
"""Write a small synthetic wezterm trace plus origin files with known latencies.

    gen_fixture.py [OUTDIR]   (default: this directory)

Produces trace.txt, inject.txt and evdev.txt. The expected analyze.py
results are checked by test_analyze.py. All offsets below are microseconds.

  chain 1  inject seq 1, plain path, presented            total 10.0 ms
  chain 2  inject seq 2, deferred paint, a stale paint      total 20.0 ms
           (seqno 10 < 11) in between that must be skipped
  chain 3  inject seq 3, frame discarded
  chain 4  evdev press, modifier: no pty_write before the next press
  chain 5  no origin record (origin = wl_key), presented    total 8.8 ms
  output   pty_read at +400 ms with no preceding pty_write  total 8.0 ms
  late     pty_read at +500 ms whose parse_apply is recorded during the
           paint that already shows its seqno (blocked on the terminal
           lock), and gui_output after that paint; the chain must attach
           to that paint, not the next one                 total 7.0 ms
  burst    pty_reads at +600 ms and +600.1 ms; the parser is still on the
           first when the second arrives, so the first parse_apply after
           the second read (seqno 16) covers only the first read. The
           first chain is normal (total 8.0 ms); the second is ambiguous
           and must be excluded from the stage statistics
  startup  pty_read at -20 ms, before the first commit (window not yet
           shown); output mode must exclude it
  stray    inject seq 6 at +300 ms with no wl_key (unmatched origin)
"""

import os
import random
import sys

BASE = 1_000_000_000_000  # 1000 s of CLOCK_MONOTONIC, in ns


def us(x):
    return BASE + int(round(x * 1000))


def build():
    trace = [(BASE - 5_000_000, "pres_clock", [1])]
    inject = []
    evdev = []

    def t(at_us, name, *vals):
        trace.append((us(at_us), name, list(vals)))

    def wl(at_us, origin_us, pressed=1):
        t(at_us, "wl_key", us(origin_us) // 1_000_000, pressed)

    # startup output, read before the window is first shown (first commit)
    b = -20_000
    t(b, "pty_read", 0, 50)
    t(b + 100, "parse_apply", 0, 1)
    t(b + 200, "gui_output", 0)
    t(b + 250, "invalidate", 1, 0)
    t(b + 5000, "paint_start")
    t(b + 5100, "paint_pane", 0, 1)
    t(b + 5500, "paint_end")
    t(b + 6000, "commit", 1, 0)

    # chain 1
    b = 0
    inject.append((us(b), "inject_key", [1]))
    wl(b + 300, b)
    t(b + 500, "pty_write", 0, 1)
    t(b + 1500, "pty_read", 0, 2)
    t(b + 1600, "parse_apply", 0, 10)
    t(b + 1800, "gui_output", 0)
    t(b + 1850, "invalidate", 1, 0)
    t(b + 2000, "paint_start")
    t(b + 2100, "paint_pane", 0, 10)
    t(b + 4000, "paint_end")
    t(b + 4200, "commit", 1, 101)
    t(b + 9000, "frame_cb", 1)
    t(b + 12000, "presented", 101, us(b + 10000), 16_666_666, 7)
    wl(b + 20000, b + 19700, 0)

    # chain 2
    b = 50_000
    inject.append((us(b), "inject_key", [2]))
    wl(b + 400, b)
    t(b + 600, "pty_write", 0, 1)
    t(b + 2600, "pty_read", 0, 2)
    t(b + 2700, "parse_apply", 0, 11)
    # stale paint already in flight: shows seqno 10, must be skipped
    t(b + 2800, "paint_start")
    t(b + 2850, "paint_pane", 0, 10)
    t(b + 2900, "paint_end")
    t(b + 2950, "commit", 1, 0)
    t(b + 3000, "gui_output", 0)
    t(b + 3050, "invalidate", 1, 1)
    t(b + 8000, "frame_cb", 1)
    t(b + 8100, "paint_start")
    t(b + 8200, "paint_pane", 0, 11)
    t(b + 10100, "paint_end")
    t(b + 10400, "commit", 1, 102)
    t(b + 21000, "presented", 102, us(b + 20000), 16_666_666, 7)

    # chain 3
    b = 100_000
    inject.append((us(b), "inject_key", [3]))
    wl(b + 500, b)
    t(b + 700, "pty_write", 0, 1)
    t(b + 1700, "pty_read", 0, 2)
    t(b + 1800, "parse_apply", 0, 12)
    t(b + 1900, "gui_output", 0)
    t(b + 1950, "invalidate", 1, 0)
    t(b + 2000, "paint_start")
    t(b + 2100, "paint_pane", 0, 12)
    t(b + 3000, "paint_end")
    t(b + 3100, "commit", 1, 103)
    t(b + 20000, "discarded", 103)

    # chain 4: modifier press seen by evdev, writes nothing
    b = 150_000
    evdev.append((us(b), "evdev_key", [1]))
    evdev.append((us(b + 90_000), "evdev_key", [0]))
    wl(b + 300, b)

    # chain 5: no origin record
    b = 200_000
    t(b, "wl_key", us(b) // 1_000_000, 1)
    t(b + 200, "pty_write", 0, 1)
    t(b + 1200, "pty_read", 0, 2)
    t(b + 1300, "parse_apply", 0, 13)
    t(b + 1400, "gui_output", 0)
    t(b + 1450, "invalidate", 1, 0)
    t(b + 1500, "paint_start")
    t(b + 1600, "paint_pane", 0, 13)
    t(b + 2500, "paint_end")
    t(b + 2600, "commit", 1, 105)
    t(b + 9500, "presented", 105, us(b + 9000), 16_666_666, 7)

    # stray injected key that never reached wezterm
    inject.append((us(300_000), "inject_key", [6]))

    # output-only update
    b = 400_000
    t(b, "pty_read", 0, 10)
    t(b + 100, "parse_apply", 0, 14)
    t(b + 200, "gui_output", 0)
    t(b + 250, "invalidate", 1, 0)
    t(b + 300, "paint_start")
    t(b + 400, "paint_pane", 0, 14)
    t(b + 1300, "paint_end")
    t(b + 1400, "commit", 1, 106)
    t(b + 8500, "presented", 106, us(b + 8000), 16_666_666, 7)

    # output-only update whose parse_apply lands inside the paint showing it
    b = 500_000
    t(b, "pty_read", 0, 20)
    t(b + 100, "paint_start")
    t(b + 200, "paint_pane", 0, 15)
    t(b + 300, "parse_apply", 0, 15)  # seqno read waited for the paint
    t(b + 1000, "paint_end")
    t(b + 1100, "commit", 1, 107)
    t(b + 1200, "gui_output", 0)
    t(b + 1250, "invalidate", 1, 0)
    t(b + 1300, "paint_start")
    t(b + 1400, "paint_pane", 0, 15)  # repaint, must not be chosen
    t(b + 2000, "paint_end")
    t(b + 2100, "commit", 1, 108)
    t(b + 7500, "presented", 107, us(b + 7000), 16_666_666, 7)
    t(b + 24000, "presented", 108, us(b + 23666), 16_666_666, 7)

    # burst: second read arrives while the parser is still on the first
    b = 600_000
    t(b, "pty_read", 0, 30)
    t(b + 100, "pty_read", 0, 40)  # no parse_apply since the previous read
    t(b + 300, "parse_apply", 0, 16)  # covers only the first read
    t(b + 400, "gui_output", 0)
    t(b + 450, "invalidate", 1, 0)
    t(b + 500, "paint_start")
    t(b + 600, "paint_pane", 0, 16)
    t(b + 1500, "paint_end")
    t(b + 1600, "commit", 1, 109)
    t(b + 1700, "parse_apply", 0, 17)  # covers the second read
    t(b + 1800, "gui_output", 0)
    t(b + 1850, "invalidate", 1, 0)
    t(b + 2000, "paint_start")
    t(b + 2100, "paint_pane", 0, 17)
    t(b + 3000, "paint_end")
    t(b + 3100, "commit", 1, 110)
    t(b + 8500, "presented", 109, us(b + 8000), 16_666_666, 7)
    t(b + 25000, "presented", 110, us(b + 24666), 16_666_666, 7)

    return trace, inject, evdev


def write(path, header, records, shuffle=False):
    lines = ["%d,%s%s" % (ts, name, "".join("," + str(v) for v in vals))
             for ts, name, vals in sorted(records)]
    if shuffle:
        # Records from different threads are not strictly ordered in a real
        # trace; perturb neighbours so the analyzer must sort.
        rng = random.Random(1)
        for i in range(0, len(lines) - 1, 3):
            if rng.random() < 0.5:
                lines[i], lines[i + 1] = lines[i + 1], lines[i]
    with open(path, "w") as f:
        f.write(header + "\n")
        f.write("\n".join(lines) + "\n")


def main(outdir):
    trace, inject, evdev = build()
    write(os.path.join(outdir, "trace.txt"),
          "# wezterm latency trace pid=1 clock=CLOCK_MONOTONIC (synthetic)",
          trace, shuffle=True)
    write(os.path.join(outdir, "inject.txt"),
          "# inject_keys.py (synthetic)", inject)
    write(os.path.join(outdir, "evdev.txt"),
          "# sniff_evdev.py (synthetic)", evdev)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1
         else os.path.dirname(os.path.abspath(__file__)))
