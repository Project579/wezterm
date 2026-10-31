#!/usr/bin/env python3
"""Repaint the first row of the terminal on every read of keyboard input.

    echo_probe.py [--ack FILE] [--count N]

Run inside a wezterm pane. The tty is put in raw mode; each read of input
bytes toggles row 1 between a full row of '#' and a row of spaces with a
single write (cursor home first), so every key press produces exactly one
visible change for the latency trace to follow.

--ack FILE   after each repaint, write the number of reads so far to FILE
             (atomically replaced). inject_keys.py waits on it before
             sending the next key. The file is set to 0 at start.
--count N    exit after N reads.

Ctrl-C (byte 0x03) exits. The tty is restored on exit.
"""

import argparse
import os
import sys
import termios
import time
import tty


def write_ack(path, n):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("%d\n" % n)
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(
        description="Toggle row 1 on every keyboard read (latency probe).")
    p.add_argument("--ack", metavar="FILE",
                   help="write the running read count here after each repaint")
    p.add_argument("--delay-ms", type=float, default=0.0,
                   help="wait this long before responding to each read, to "
                        "imitate a program that takes time to echo")
    p.add_argument("--count", type=int, default=0,
                   help="exit after N reads (default: run until Ctrl-C)")
    args = p.parse_args()

    fd_in = sys.stdin.fileno()
    fd_out = sys.stdout.fileno()
    if not os.isatty(fd_in):
        sys.exit("stdin is not a tty")
    cols = os.get_terminal_size(fd_out).columns or 80
    rows = [b"\x1b[H" + b"#" * cols, b"\x1b[H" + b" " * cols]

    saved = termios.tcgetattr(fd_in)
    count = 0
    try:
        tty.setraw(fd_in)
        # Clear, hide the cursor, print a hint on row 3.
        os.write(fd_out, b"\x1b[2J\x1b[?25l\x1b[3;1Hecho_probe: each key "
                 b"toggles row 1. Ctrl-C to quit.")
        if args.ack:
            write_ack(args.ack, 0)
        while True:
            data = os.read(fd_in, 4096)
            if not data or b"\x03" in data:
                break
            if args.delay_ms:
                time.sleep(args.delay_ms / 1000)
            os.write(fd_out, rows[count % 2])
            count += 1
            if args.ack:
                write_ack(args.ack, count)
            if args.count and count >= args.count:
                break
    finally:
        termios.tcsetattr(fd_in, termios.TCSAFLUSH, saved)
        os.write(fd_out, b"\x1b[?25h\x1b[2J\x1b[H")
    print("echo_probe: %d reads" % count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
