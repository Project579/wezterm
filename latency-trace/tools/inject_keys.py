#!/usr/bin/env python3
"""Type one key N times through a uinput virtual keyboard, with a handshake.

    inject_keys.py --ack FILE [-n 200] [--key KEY_A] [-o inject.txt]
                   [--countdown 5] [--min-ms 30] [--max-ms 80]
                   [--allow-unsafe-key]

Creates a virtual keyboard via /dev/uinput (needs write access: the `input`
group on most systems), counts down so you can focus the wezterm window that
runs `echo_probe.py --ack FILE`, then sends press+release of one key N times
at random intervals of uniform(min-ms, max-ms) milliseconds.

Safety handshake (mandatory): before sending key k+1 the injector waits up
to --ack-timeout (1 s) for FILE to show k acknowledgements from
echo_probe.py. On timeout it aborts and destroys the device, so if focus is
on some other window at most one stray key lands there. FILE must exist
before starting (echo_probe.py creates it with 0).

To limit what that one stray key can do, --key is limited to letters,
digits and space unless --allow-unsafe-key is given (Enter or Backspace
landing in a shell or editor could run or delete something). Even a letter
is not always harmless: in vim normal mode or a TUI a single x, p, u, q or
d is a command. The default KEY_A is the most benign choice; keep it unless
you need another key. Modifiers held on a
physical keyboard would turn the key into a shortcut, so before the first
tap the injector reads the key state of every keyboard (EVIOCGKEY, the same
detection as sniff_evdev.py): if Ctrl, Alt, AltGr, Shift or Meta is held it
waits up to --countdown seconds for release and aborts if it is still held.
Before every later tap it aborts at once if a modifier is held.

Before each press it appends `<ts_ns>,inject_key,<seq>` (CLOCK_MONOTONIC)
to the output file; give that file to analyze.py as an extra input.
"""

import argparse
import fcntl
import glob
import os
import random
import struct
import sys
import time

INPUT_EVENT = struct.Struct("llHHi")  # 64-bit struct input_event
EV_SYN, EV_KEY = 0x00, 0x01
SYN_REPORT = 0
BUS_VIRTUAL = 0x06

_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


# struct uinput_setup { struct input_id id; char name[80]; __u32 ff_effects_max; }
UINPUT_SETUP = struct.Struct("HHHH80sI")
UI_DEV_CREATE = _ioc(_IOC_NONE, "U", 1, 0)
UI_DEV_DESTROY = _ioc(_IOC_NONE, "U", 2, 0)
UI_DEV_SETUP = _ioc(_IOC_WRITE, "U", 3, UINPUT_SETUP.size)
UI_SET_EVBIT = _ioc(_IOC_WRITE, "U", 100, struct.calcsize("i"))
UI_SET_KEYBIT = _ioc(_IOC_WRITE, "U", 101, struct.calcsize("i"))

# evdev key state, for the held-modifier check. Keyboard detection matches
# sniff_evdev.py: EV_KEY with all of KEY_A..KEY_Z.
KEY_MAX = 0x2FF
KEY_BYTES = KEY_MAX // 8 + 1
LETTER_CODES = list(range(16, 26)) + list(range(30, 39)) + list(range(44, 51))
EVIOCGKEY = _ioc(_IOC_READ, "E", 0x18, KEY_BYTES)
EVIOCGBIT_EV = _ioc(_IOC_READ, "E", 0x20, 4)
EVIOCGBIT_KEY = _ioc(_IOC_READ, "E", 0x20 + EV_KEY, KEY_BYTES)
# LEFTCTRL, LEFTSHIFT, RIGHTSHIFT, LEFTALT, RIGHTCTRL, RIGHTALT (AltGr),
# LEFTMETA, RIGHTMETA
MODIFIER_CODES = {29: "Left Ctrl", 42: "Left Shift", 54: "Right Shift",
                  56: "Left Alt", 97: "Right Ctrl", 100: "Right Alt/AltGr",
                  125: "Left Meta", 126: "Right Meta"}

# Linux input-event-codes.h key codes for the keys worth injecting.
KEYS = {"KEY_SPACE": 57, "KEY_ENTER": 28, "KEY_BACKSPACE": 14,
        "KEY_DOT": 52, "KEY_SLASH": 53}
for _row, _start in (("QWERTYUIOP", 16), ("ASDFGHJKL", 30), ("ZXCVBNM", 44)):
    for _i, _ch in enumerate(_row):
        KEYS["KEY_" + _ch] = _start + _i
for _i, _ch in enumerate("1234567890"):
    KEYS["KEY_" + _ch] = 2 + _i


# Keys accepted without --allow-unsafe-key: letters, digits and space. These
# only type a character into a shell prompt or text field, but they are not
# harmless everywhere: in vim normal mode or a TUI a single letter (x, p, u,
# q, d) is a command. The default KEY_A is the most benign choice.
SAFE_KEYS = {"KEY_SPACE"} | {"KEY_" + ch for ch in
                             "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
SAFE_CODES = {KEYS[k] for k in SAFE_KEYS}


def parse_key(name, allow_unsafe=False):
    if name.isdigit() and len(name) > 1:  # "7" means KEY_7, not code 7
        code = int(name)
    else:
        key = name.upper()
        if not key.startswith("KEY_"):
            key = "KEY_" + key
        if key not in KEYS:
            sys.exit("unknown key %r; use one of %s or a numeric code"
                     % (name, ", ".join(sorted(KEYS))))
        code = KEYS[key]
    if code not in SAFE_CODES and not allow_unsafe:
        sys.exit("refusing key %r: only letters, digits and space are "
                 "allowed, because one stray key may reach another window; "
                 "pass --allow-unsafe-key to override" % name)
    return code


def _has_bit(buf, bit):
    return bool(buf[bit // 8] & (1 << (bit % 8)))


def open_keyboards():
    """Read-only fds of every evdev keyboard (KEY_A..KEY_Z, as sniff_evdev)."""
    fds = []
    for path in glob.glob("/dev/input/event*"):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            evbits = fcntl.ioctl(fd, EVIOCGBIT_EV, bytes(4))
            keybits = (fcntl.ioctl(fd, EVIOCGBIT_KEY, bytes(KEY_BYTES))
                       if _has_bit(evbits, EV_KEY) else bytes(KEY_BYTES))
        except OSError:
            keybits = bytes(KEY_BYTES)
        if all(_has_bit(keybits, c) for c in LETTER_CODES):
            fds.append(fd)
        else:
            os.close(fd)
    return fds


def held_modifiers(fds):
    """Names of modifiers currently held on any of the keyboards."""
    held = set()
    for fd in fds:
        try:
            state = fcntl.ioctl(fd, EVIOCGKEY, bytes(KEY_BYTES))
        except OSError:
            continue
        held.update(name for code, name in MODIFIER_CODES.items()
                    if _has_bit(state, code))
    return sorted(held)


def wait_modifiers_released(fds, timeout):
    """True once no modifier is held, False if one still is after timeout."""
    deadline = time.monotonic() + timeout
    held = held_modifiers(fds)
    if held:
        print("modifier held (%s); release it, waiting up to %.1f s"
              % (", ".join(held), timeout), file=sys.stderr)
    while held:
        if time.monotonic() >= deadline:
            print("ABORT: modifier still held: %s" % ", ".join(held),
                  file=sys.stderr)
            return False
        time.sleep(0.01)
        held = held_modifiers(fds)
    return True


def read_ack(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return None


def wait_ack(path, want, timeout):
    deadline = time.monotonic() + timeout
    while True:
        n = read_ack(path)
        if n is not None and n >= want:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.0005)


class VirtualKeyboard:
    def __init__(self, name):
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as err:
            sys.exit(f"cannot open /dev/uinput: {err}\n"
                     "load the module with: sudo modprobe uinput")
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)
            # Advertise the whole basic keyboard range so the compositor
            # (libinput / udev input_id) treats the device as a keyboard.
            for code in range(1, 0x100):
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            setup = UINPUT_SETUP.pack(BUS_VIRTUAL, 0, 0, 1,
                                      name.encode()[:79], 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except BaseException:
            os.close(self.fd)
            raise

    def tap(self, code, hold_s):
        syn = INPUT_EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0)
        os.write(self.fd, INPUT_EVENT.pack(0, 0, EV_KEY, code, 1) + syn)
        if hold_s > 0:
            time.sleep(hold_s)
        os.write(self.fd, INPUT_EVENT.pack(0, 0, EV_KEY, code, 0) + syn)

    def close(self):
        if self.fd is not None:
            # Destroying the device also releases any key still held.
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            finally:
                os.close(self.fd)
                self.fd = None


def main():
    p = argparse.ArgumentParser(
        description="Inject key presses via uinput, acked by echo_probe.py.")
    p.add_argument("--ack", required=True, metavar="FILE",
                   help="ack file written by echo_probe.py --ack")
    p.add_argument("-n", "--count", type=int, default=200)
    p.add_argument("--key", default="KEY_A",
                   help="key to send (KEY_A..KEY_Z, KEY_0..KEY_9 or "
                        "KEY_SPACE; default KEY_A)")
    p.add_argument("--allow-unsafe-key", action="store_true",
                   help="also allow other keys (KEY_ENTER, KEY_BACKSPACE, "
                        "numeric codes, ...)")
    p.add_argument("-o", "--output", default="inject.txt",
                   help="output file (default: inject.txt)")
    p.add_argument("--countdown", type=float, default=5.0,
                   help="seconds to focus the probe window (default 5)")
    p.add_argument("--min-ms", type=float, default=30.0)
    p.add_argument("--max-ms", type=float, default=80.0)
    p.add_argument("--hold-ms", type=float, default=2.0,
                   help="time between press and release (default 2)")
    p.add_argument("--ack-timeout", type=float, default=1.0)
    args = p.parse_args()
    code = parse_key(args.key, args.allow_unsafe_key)
    if not 0 <= args.min_ms <= args.max_ms:
        sys.exit("need 0 <= --min-ms <= --max-ms")
    if read_ack(args.ack) is None:
        sys.exit("ack file %s missing or unreadable; start "
                 "`echo_probe.py --ack %s` in the wezterm pane first"
                 % (args.ack, args.ack))
    # Opened before the virtual keyboard exists, which never holds a
    # modifier anyway.
    keyboards = open_keyboards()
    if not keyboards:
        sys.exit("no readable keyboard devices, cannot check for held "
                 "modifiers (are you in the input group?)")

    kbd = VirtualKeyboard("wezterm-latency-inject")
    sent = 0
    ok = False
    try:
        for left in range(int(args.countdown), 0, -1):
            print("focus the echo_probe window: sending in %d s " % left,
                  file=sys.stderr)
            time.sleep(1)
        time.sleep(args.countdown - int(args.countdown))
        if not wait_modifiers_released(keyboards, args.countdown):
            return 1
        base = read_ack(args.ack)
        if base is None:
            sys.exit("ack file %s disappeared" % args.ack)

        with open(args.output, "w") as out:
            out.write("# inject_keys.py clock=CLOCK_MONOTONIC key=%d\n" % code)
            last_press = 0.0
            for seq in range(1, args.count + 1):
                if seq > 1:
                    if not wait_ack(args.ack, base + seq - 1, args.ack_timeout):
                        print("\nABORT: no ack for key %d within %.1f s; "
                              "focus is probably not on echo_probe"
                              % (seq - 1, args.ack_timeout), file=sys.stderr)
                        return 1
                    target = last_press + random.uniform(
                        args.min_ms, args.max_ms) / 1000.0
                    delay = target - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    held = held_modifiers(keyboards)
                    if held:
                        print("\nABORT: modifier held: %s" % ", ".join(held),
                              file=sys.stderr)
                        return 1
                out.write("%d,inject_key,%d\n"
                          % (time.clock_gettime_ns(time.CLOCK_MONOTONIC), seq))
                last_press = time.monotonic()
                kbd.tap(code, args.hold_ms / 1000.0)
                sent = seq
            ok = wait_ack(args.ack, base + sent, args.ack_timeout)
            if not ok:
                print("\nwarning: last key was not acknowledged",
                      file=sys.stderr)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        kbd.close()
        for fd in keyboards:
            os.close(fd)
        print("sent %d key presses; log in %s" % (sent, args.output),
              file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
