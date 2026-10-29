#!/usr/bin/env python3
"""Repaint the first row of the terminal at random intervals, without input.

    output_probe.py [--count N] [--min-ms 30] [--max-ms 80] [--delay S]

Run inside a wezterm pane. Every uniform(min-ms, max-ms) milliseconds, row 1
is toggled between a full row of '#' and a row of spaces with a single write
(cursor home first), N times. Analyze the trace with
`analyze.py TRACE --mode output` to measure output-to-photon latency.
Ctrl-C stops early.
"""

import argparse
import os
import random
import sys
import termios
import time


def main():
    p = argparse.ArgumentParser(
        description="Toggle row 1 at random intervals (output latency probe).")
    p.add_argument("--count", type=int, default=500,
                   help="number of repaints (default 500)")
    p.add_argument("--min-ms", type=float, default=30.0)
    p.add_argument("--max-ms", type=float, default=80.0)
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds to wait before the first repaint (default 1)")
    args = p.parse_args()
    if not 0 <= args.min_ms <= args.max_ms:
        sys.exit("need 0 <= --min-ms <= --max-ms")

    fd_out = sys.stdout.fileno()
    if not os.isatty(fd_out):
        sys.exit("stdout is not a tty")
    cols = os.get_terminal_size(fd_out).columns or 80
    rows = [b"\x1b[H" + b"#" * cols, b"\x1b[H" + b" " * cols]

    # Turn off echo so stray keys do not add repaints of their own.
    fd_in = sys.stdin.fileno()
    saved = termios.tcgetattr(fd_in) if os.isatty(fd_in) else None
    if saved:
        attrs = termios.tcgetattr(fd_in)
        attrs[3] &= ~(termios.ECHO | termios.ICANON)
        termios.tcsetattr(fd_in, termios.TCSANOW, attrs)

    n = 0
    try:
        os.write(fd_out, b"\x1b[2J\x1b[?25l\x1b[3;1Houtput_probe: %d repaints, "
                 b"Ctrl-C to stop." % args.count)
        time.sleep(args.delay)
        while n < args.count:
            time.sleep(random.uniform(args.min_ms, args.max_ms) / 1000.0)
            os.write(fd_out, rows[n % 2])
            n += 1
    except KeyboardInterrupt:
        pass
    finally:
        if saved:
            termios.tcsetattr(fd_in, termios.TCSAFLUSH, saved)
        os.write(fd_out, b"\x1b[?25h\x1b[2J\x1b[H")
    print("output_probe: %d repaints" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
