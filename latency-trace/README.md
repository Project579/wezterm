# latency-trace

Opt-in tracing of wezterm's input-to-photon pipeline, plus Python tools
(stdlib only, Linux only) to drive and analyze it.

## What it measures

With `WEZTERM_LATENCY_TRACE=/path/to/file` set, wezterm appends one line per
pipeline event to that file:

```text
<ts_ns>,<event>,<value>,<value>,...
```

Timestamps are `CLOCK_MONOTONIC` nanoseconds. Lines starting with `#` are
comments. Records come from several threads, so the file is only roughly in
timestamp order; `analyze.py` sorts it. The events are defined in
`src/lib.rs`:

| event | values | meaning |
|---|---|---|
| `wl_key` | wl_time_ms, pressed | `wl_keyboard.key` arrived |
| `pty_write` | pane, bytes | pty write returned |
| `pty_read` | pane, bytes | pty read returned |
| `parse_apply` | pane, seqno | parsed actions applied to the model |
| `gui_output` | pane | GUI thread handled the output notification |
| `invalidate` | window, deferred | window invalidated; deferred=1 means the paint waits for a frame callback |
| `paint_start` | | paint began |
| `paint_pane` | pane, seqno | pane painted; `seqno` is a lower bound on the painted content (it shows at least that seqno, possibly newer) |
| `paint_end` | | drawing finished, before present / buffer swap |
| `commit` | window, frame_id | frame handed to the compositor: right after `eglSwapBuffers` (OpenGL) or after `present()` (WebGpu) |
| `frame_cb` | window | `wl_surface.frame` callback fired |
| `presented` | frame_id, presented_ns, refresh_ns, flags | `wp_presentation_feedback.presented` |
| `discarded` | frame_id | `wp_presentation_feedback.discarded` |
| `pres_clock` | clk_id | `wp_presentation.clock_id` |

For key input, `analyze.py` reports the latency of each stage:

```text
origin            key press: kernel evdev time (sniff_evdev.py) or injection time (inject_keys.py)
 -> wl_key        compositor delivered the key to wezterm
 -> pty_write     wezterm wrote the bytes to the pty
 -> pty_read      the application echoed (pty_write -> pty_read is the app's time, not wezterm's)
 -> parse_apply   wezterm parsed the echo into the terminal model
 -> gui_output    the GUI thread was notified
 -> paint_start   painting began (deferred paints wait here for a frame callback)
 -> paint_end     drawing finished (before present / buffer swap)
 -> commit        eglSwapBuffers (OpenGL) or present() (WebGpu) returned
 -> presented     the compositor reports the frame reached the screen
```

`presented` is the `presented_ns` value from `wp_presentation`, which is when
the display started scanning the frame out, not when wezterm received the
feedback.

## Enabling

```sh
WEZTERM_LATENCY_TRACE="$XDG_RUNTIME_DIR/trace.txt" \
  wezterm start --always-new-process -- \
  python3 latency-trace/tools/echo_probe.py --ack "$XDG_RUNTIME_DIR/ack"
```

`--always-new-process` makes sure the variable reaches a fresh GUI process
instead of an already running wezterm. At startup wezterm-gui removes
`WEZTERM_LATENCY_TRACE` from its own environment, so no child process
inherits it: neither the programs running in panes nor an auto-spawned mux
server. When it is unset, tracing costs one atomic load per event site.

Tracing is available on unix only; elsewhere the variable is ignored. The
trace file is created with mode 0600 and opened, truncating it, the first time
tracing is used (wezterm-gui does so when it connects to the Wayland
compositor), so a short-lived process such as `wezterm ls-fonts`, or a
`wezterm start` that hands off to a running GUI, leaves it alone.
Records are buffered and the writer flushes no later than 50 ms after a
record arrives, so records from the last ~50 ms before wezterm exits may be
missing from the file.

The examples keep all files in `$XDG_RUNTIME_DIR` (normally
`/run/user/$UID`, mode 0700) rather than `/tmp`, because traces and evdev
recordings contain keystroke timing (see Security).

## Recipes

All tools take `--help`. Each run writes a new file, so start wezterm fresh
(or point the variable at a new file) for every measurement.

### Manual typing (sniff_evdev.py)

Records the kernel timestamp of every physical key press and release, so the
analysis includes the compositor's share.

```sh
D="$XDG_RUNTIME_DIR"
# terminal 1 (any terminal): record evdev timestamps
python3 latency-trace/tools/sniff_evdev.py -o "$D/evdev.txt"
# terminal 2: the traced wezterm running the probe
WEZTERM_LATENCY_TRACE="$D/trace.txt" wezterm start --always-new-process -- \
  python3 latency-trace/tools/echo_probe.py
# type in the probe window, Ctrl-C the probe, then Ctrl-C the sniffer
python3 latency-trace/tools/analyze.py "$D/trace.txt" "$D/evdev.txt"
```

`sniff_evdev.py` auto-detects keyboards (devices reporting KEY_A..KEY_Z) or
takes `/dev/input/eventN` paths. It never grabs the device. Run
`sniff_evdev.py --check` to list the devices and confirm the clock can be set,
without recording. Key presses typed into other windows show up as "origins
without a matching wl_key" and are otherwise ignored. The output file is
created with mode 0600.

Without an evdev file, `analyze.py "$D/trace.txt"` uses the `wl_key` event
as the origin of each chain.

### Automated typing (inject_keys.py)

Types one key through a uinput virtual keyboard. Needs write access to
`/dev/uinput` (on this machine `root:input`, mode 660, so the `input` group).

```sh
D="$XDG_RUNTIME_DIR"
WEZTERM_LATENCY_TRACE="$D/trace.txt" wezterm start --always-new-process -- \
  python3 latency-trace/tools/echo_probe.py --ack "$D/ack"
# from another terminal, then focus the probe window during the countdown
python3 latency-trace/tools/inject_keys.py --ack "$D/ack" -n 500 \
  -o "$D/inject.txt"
python3 latency-trace/tools/analyze.py "$D/trace.txt" "$D/inject.txt" \
  --csv "$D/chains.csv"
```

The injector counts down 5 s (`--countdown`), then sends press+release of
`--key` (default `KEY_A`) at random intervals of 30-80 ms (`--min-ms`,
`--max-ms`). The handshake is mandatory: before each key after the first it
waits up to 1 s for `echo_probe.py` to acknowledge the previous one through
the ack file. If focus is not on the probe, the first key goes astray, no ack
arrives, and the injector aborts and destroys the device, so at most one
stray key reaches another window.

To limit what that stray key can do:

- `--key` accepts only letters, digits and space (`KEY_A`..`KEY_Z`,
  `KEY_0`..`KEY_9`, `KEY_SPACE`); anything else, such as `KEY_ENTER`,
  `KEY_BACKSPACE` or a numeric code, needs `--allow-unsafe-key`.
- Before the first key the injector reads the key state of every keyboard
  (`EVIOCGKEY`, same detection as `sniff_evdev.py`, so it needs read access
  to `/dev/input/event*` too). If Ctrl, Alt, AltGr, Shift or Meta (left or
  right) is held, it waits up to the countdown length for release and aborts
  if it is still held. Before each later key it aborts at once if a modifier
  is held.

Residual risk: the stray key can still type a letter, digit or space into
whatever has focus (a text field, a shell prompt, a game, a "press any key"
dialog), and a letter is not harmless everywhere: in vim normal mode or a TUI
a single letter is a command (`x` deletes, `p` pastes, `u` undoes, `q` quits
or starts a macro, `d` starts a delete), and so it is in mail clients and web
pages with keyboard shortcuts. Keep the default `KEY_A`, the most benign
choice, unless you need another key. The
modifier check reads only evdev keyboards: modifiers latched or locked in the
compositor (sticky keys, a Caps Lock remapped to Ctrl in software), or held
on a device that does not report KEY_A..KEY_Z, are not seen, and a modifier
pressed between the check and the key is not either. Keep focus on the probe
and hands off the keyboard while it runs.

### Output only (output_probe.py)

Measures the path from the pty to the screen without any input.

```sh
D="$XDG_RUNTIME_DIR"
WEZTERM_LATENCY_TRACE="$D/trace.txt" wezterm start --always-new-process -- \
  python3 latency-trace/tools/output_probe.py --count 500
python3 latency-trace/tools/analyze.py "$D/trace.txt" --mode output
```

The probe toggles row 1 every uniform(30, 80) ms. In output mode each
`pty_read` starts a chain, except reads that follow a `pty_write` on the same
pane within 50 ms (`--echo-window-ms`, 0 keeps them all) and reads before the
first `commit` in the trace. The latter are output produced before the window
was first shown, whose chains measure startup rather than the output path;
the report prints how many were excluded.

## Reading the report

For each stage `analyze.py` prints n, p50, p95, p99 and max in milliseconds,
counted over the chains that reached both ends of the stage. It also prints
the number of deferred paints (an `invalidate` with deferred=1 between
`gui_output` and `paint_start`, meaning the paint waited for a frame
callback), discarded frames, ambiguous chains (see below; not counted in the
stage statistics), and chains that did not reach `presented`, by the stage
where they stopped (`no_pty_write` is normal for modifier keys). `--csv`
writes every chain's timestamps, ambiguous ones included, for your own
analysis.

How chains are matched:

- An origin (`inject_key`, or an `evdev_key` press) is paired with the
  `wl_key` press whose `wl_time_ms` is within 1 of `floor(origin_ns / 1e6)`,
  the nearest one first. libinput derives `wl_time_ms` from the evdev
  timestamp.
- The first `pty_write` after the `wl_key`, before the next key press, then
  the first `pty_read` on that pane and the first `parse_apply` on that pane
  after it (its seqno S). The reader and the parser are separate threads and
  the parser coalesces queued reads, so when the parser is behind, that
  `parse_apply` can belong to an earlier read still in the queue; S is then
  too low and may match a paint that does not contain this read, which
  underestimates latency under bursty output. A chain is marked *ambiguous*
  when the previous `pty_read` on the same pane has no `parse_apply` (same
  pane) between it and the chain's own `pty_read`. Ambiguous chains are left
  out of the stage statistics; the report prints how many there were and
  `--csv` flags them in the `ambiguous` column.
- From the `pty_read` on (not from `parse_apply`, see below): the first
  `gui_output` for that pane, and the first `paint_pane` for that pane with
  seqno >= S. Since a `paint_pane` seqno is a lower bound on the painted
  content, seqno >= S proves that paint shows the new content. Its enclosing
  `paint_start` and `paint_end` delimit the paint.
- The commit is the first one after that `paint_end`, provided no other paint
  started in between (paint and commit run in sequence on the GUI thread).
  Its frame_id leads to `presented` or `discarded`.

Stages can be negative, and are reported as they are, not clamped:

- `gui_output -> paint_start` when a paint already in progress picked up the
  new content.
- `parse_apply -> gui_output` and `gui_output -> paint_start` (with
  `parse_apply` after `paint_start`) when `parse_apply` is recorded late. Its
  timestamp is taken right after the parser applies its actions and releases
  the terminal lock; if the parser thread is preempted in between, a paint
  can take the lock and show the new content first, so `parse_apply` can
  land inside or after the paint that displayed it. This is why the later
  stages are matched from `pty_read`.

`analyze.py` warns when `pres_clock` is not 1 (`CLOCK_MONOTONIC`), because
then `presented_ns` cannot be compared with the other timestamps.

## What is not covered

- The keyboard itself: matrix scan, debounce and the USB polling interval
  (up to 1 ms at 1000 Hz, 8 ms at 125 Hz) happen before the kernel timestamp.
  Injected keys skip this entirely.
- The display: `presented` is the start of scanout of the frame, as reported
  by `wp_presentation`. Where the probe row sits on the panel (row 1 is
  scanned out first), the panel's processing and pixel response time all come
  after it. A photodiode or high-speed camera is needed for those.
- Compositor internals between the commit and scanout are visible only as
  the total `commit -> presented`.

## Security

`sniff_evdev.py` reads every key typed on the selected keyboards, in every
application, while it runs. It writes only the timestamp and whether the event
was a press or release; it never records key codes, device names or anything
else that identifies which key was pressed. Keystroke timing alone can still
reveal something about what was typed, so do not type passwords or other
secrets while it runs, and delete the output when you are done. It creates
the output with mode 0600, and wezterm creates the trace (whose `wl_key`
records carry the same timing) with mode 0600 too; still keep both in a
private directory such as `$XDG_RUNTIME_DIR`, not in `/tmp`.

`inject_keys.py` types into whatever window has focus; see the handshake,
key restrictions and residual risk above.

## Files

- `src/lib.rs`: the tracer used by wezterm.
- `tools/sniff_evdev.py`: passive evdev key timestamp recorder.
- `tools/echo_probe.py`: repaints row 1 on every input read, writes the ack file.
- `tools/output_probe.py`: repaints row 1 at random intervals.
- `tools/inject_keys.py`: uinput key injector with the ack handshake.
- `tools/analyze.py`: per-stage latency report and CSV export.
- `tools/testdata/`: synthetic fixture (`gen_fixture.py` regenerates the
  `.txt` files) and `test_analyze.py`, run with
  `python3 latency-trace/tools/testdata/test_analyze.py`.
