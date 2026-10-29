#!/usr/bin/env python3
"""Passively record keyboard press/release timestamps from evdev.

    sniff_evdev.py [-o OUT] [--check] [/dev/input/eventN ...]

Reads keyboard evdev devices without grabbing them (no EVIOCGRAB), so every
key still reaches the compositor as usual. With no paths, every device that
reports EV_KEY with all of KEY_A..KEY_Z is used. EVIOCSCLOCKID switches each
device to CLOCK_MONOTONIC so the timestamps line up with a wezterm latency
trace. Writes until Ctrl-C:

    <ts_ns>,evdev_key,<pressed>

with pressed 1 for press and 0 for release (autorepeat, value 2, is
ignored). Merge the file into analyze.py as an extra input.

SECURITY: the output never contains key codes, device names or anything
else that says WHICH key was pressed; only the kernel timestamp and whether
it was a press or release. Keystroke timing alone can still reveal something
about what was typed (typing rhythm), so do not type passwords or other
secrets while this runs, and delete the file when you are done.

Needs read access to /dev/input/event* (membership of the `input` group).
--check opens the devices, sets the clock, waits up to 0.5 s and prints the
devices it would use, without recording anything.
"""

import argparse
import fcntl
import glob
import os
import select
import struct
import sys
import time

# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
# On 64-bit Linux struct timeval is two longs (tv_sec, tv_usec): 24 bytes.
INPUT_EVENT = struct.Struct("llHHi")

EV_KEY = 0x01
KEY_MAX = 0x2FF
BTN_MISC = 0x100  # codes below this are keyboard keys, above are buttons
LETTER_CODES = list(range(16, 26)) + list(range(30, 39)) + list(range(44, 51))

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def EVIOCGBIT(ev, length):
    return _ioc(_IOC_READ, "E", 0x20 + ev, length)


def EVIOCGNAME(length):
    return _ioc(_IOC_READ, "E", 0x06, length)


EVIOCSCLOCKID = _ioc(_IOC_WRITE, "E", 0xA0, struct.calcsize("i"))
CLOCK_MONOTONIC = time.CLOCK_MONOTONIC


def has_bit(buf, bit):
    return bool(buf[bit // 8] & (1 << (bit % 8)))


def is_keyboard(fd):
    try:
        evbits = fcntl.ioctl(fd, EVIOCGBIT(0, 4), bytes(4))
        if not has_bit(evbits, EV_KEY):
            return False
        n = KEY_MAX // 8 + 1
        keybits = fcntl.ioctl(fd, EVIOCGBIT(EV_KEY, n), bytes(n))
    except OSError:
        return False
    return all(has_bit(keybits, c) for c in LETTER_CODES)


def device_name(fd):
    try:
        raw = fcntl.ioctl(fd, EVIOCGNAME(256), bytes(256))
        return raw.split(b"\0", 1)[0].decode(errors="replace")
    except OSError:
        return "?"


def open_devices(paths):
    """Open (path, fd) for each keyboard; paths=None means auto-detect."""
    auto = not paths
    if auto:
        paths = sorted(glob.glob("/dev/input/event*"),
                       key=lambda p: int(p.rsplit("event", 1)[1]))
    devices = []
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            if not auto:
                sys.exit("cannot open %s: %s" % (path, e))
            continue
        if auto and not is_keyboard(fd):
            os.close(fd)
            continue
        try:
            fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
        except OSError as e:
            os.close(fd)
            sys.exit("EVIOCSCLOCKID failed on %s: %s" % (path, e))
        devices.append((path, fd))
    return devices


def main():
    p = argparse.ArgumentParser(
        description="Record keyboard press/release timestamps (no key codes).")
    p.add_argument("paths", nargs="*",
                   help="evdev devices (default: auto-detect keyboards)")
    p.add_argument("-o", "--output", default="evdev.txt",
                   help="output file (default: evdev.txt)")
    p.add_argument("--check", action="store_true",
                   help="open devices, set the clock, record nothing")
    args = p.parse_args()

    devices = open_devices(args.paths)
    if not devices:
        sys.exit("no keyboard devices found (are you in the input group?)")
    for path, fd in devices:
        print("using %s (%s), clock CLOCK_MONOTONIC" % (path, device_name(fd)),
              file=sys.stderr)

    fds = {fd: path for path, fd in devices}
    if args.check:
        # Drain whatever arrives in 0.5 s without storing it anywhere.
        r, _, _ = select.select(list(fds), [], [], 0.5)
        for fd in r:
            os.read(fd, INPUT_EVENT.size * 64)
        for fd in fds:
            os.close(fd)
        print("check OK: %d device(s)" % len(fds), file=sys.stderr)
        return 0

    count = 0
    # 0600: the output is keystroke timing; keep it private to this user.
    # fchmod covers an existing file, whose mode O_CREAT leaves alone.
    out_fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
    os.fchmod(out_fd, 0o600)
    with os.fdopen(out_fd, "w") as out:
        out.write("# sniff_evdev.py clock=CLOCK_MONOTONIC "
                  "(timestamps only, no key codes)\n")
        out.flush()
        print("recording to %s, Ctrl-C to stop" % args.output, file=sys.stderr)
        try:
            while fds:
                r, _, _ = select.select(list(fds), [], [], 0.5)
                if not r:
                    out.flush()
                    continue
                for fd in r:
                    try:
                        data = os.read(fd, INPUT_EVENT.size * 64)
                    except BlockingIOError:
                        continue
                    except OSError as e:
                        print("%s: %s, dropping" % (fds[fd], e),
                              file=sys.stderr)
                        os.close(fd)
                        del fds[fd]
                        continue
                    for off in range(0, len(data) - INPUT_EVENT.size + 1,
                                     INPUT_EVENT.size):
                        sec, usec, typ, code, value = \
                            INPUT_EVENT.unpack_from(data, off)
                        if typ != EV_KEY or code >= BTN_MISC or value > 1:
                            continue
                        # Deliberately not written: code.
                        out.write("%d,evdev_key,%d\n"
                                  % (sec * 1_000_000_000 + usec * 1000, value))
                        count += 1
        except KeyboardInterrupt:
            pass
    for fd in fds:
        os.close(fd)
    print("\nwrote %d events to %s" % (count, args.output), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
