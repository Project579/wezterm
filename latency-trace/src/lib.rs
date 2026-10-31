//! Opt-in tracing of the input-to-photon pipeline.
//!
//! Set `WEZTERM_LATENCY_TRACE=/path/to/file` to enable. Each call to
//! [`record`] then appends one line to that file, which is opened (and
//! truncated) the first time tracing is used: the first record, or the first
//! [`enabled`] check, which the Wayland backend makes when it connects:
//!
//! ```text
//! <timestamp_ns>,<event>,<value>,<value>,...
//! ```
//!
//! Timestamps are `CLOCK_MONOTONIC` nanoseconds, the clock that the kernel
//! evdev layer (with `EVIOCSCLOCKID`) and `wp_presentation` both report, so
//! traces from the tools in `latency-trace/tools` merge with this one by
//! timestamp alone.
//!
//! The file is created with mode 0600, since `wl_key` records carry
//! keystroke timing.
//!
//! When the variable is unset, [`record`] costs one atomic load. Records are
//! handed to a writer thread over a channel so that no file I/O happens on
//! the thread being measured. The writer flushes no later than 50ms after a
//! record arrives; it is never shut down, so records from the last ~50ms
//! before the process exits, whether normally or by crashing, may be lost.
//!
//! Binaries call [`take_env`] at the top of `main` so that the processes they
//! spawn do not inherit the variable and truncate the same file.
//!
//! Tracing is implemented on unix only, as only the Wayland backend reports
//! commits and presentation. Elsewhere the same API compiles to no-ops:
//! [`enabled`] returns false, [`record`] and [`record_at`] do nothing and
//! [`now_ns`] returns 0.

/// A point in the pipeline. The values that accompany each event are listed
/// on its variant, in the order they are written; the analyzer in
/// `latency-trace/tools/analyze.py` relies on this layout.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    /// A `wl_keyboard.key` event arrived. `[wl_time_ms, pressed]`, where
    /// `wl_time_ms` is the compositor's millisecond input timestamp and
    /// `pressed` is 1 for press, 0 for release.
    WlKey,
    /// The pty writer returned from writing bytes to the pty. `[pane_id, bytes]`
    PtyWrite,
    /// The pty reader thread returned from reading the pty. `[pane_id, bytes]`
    PtyRead,
    /// The parser applied a batch of actions to the terminal model.
    /// `[pane_id, seqno]`, `seqno` being the pane's seqno afterwards.
    ParseApply,
    /// The GUI thread handled the pane output notification. `[pane_id]`
    GuiOutput,
    /// A window was invalidated. `[window_id, deferred]`, `deferred` being 1
    /// when the paint must wait for a pending frame callback, 0 when it is
    /// performed right away.
    Invalidate,
    /// Painting of a window began. `[]`
    PaintStart,
    /// A pane was painted as part of the current paint. `[pane_id, seqno]`,
    /// `seqno` being the pane's seqno read just before its lines were
    /// fetched for painting; the painted content reflects at least this
    /// seqno.
    PaintPane,
    /// Drawing of a window finished, before the frame is presented. Recorded
    /// once per paint, by both the OpenGL and the WebGpu front ends. `[]`
    PaintEnd,
    /// The painted frame was presented, committing the surface: recorded
    /// after `eglSwapBuffers` returns for OpenGL, or after
    /// `SurfaceTexture::present` returns for WebGpu. Wayland only.
    /// `[window_id, frame_id]`, `frame_id` matching the `Presented` or
    /// `Discarded` event for this commit, or 0 when no presentation feedback
    /// was requested.
    Commit,
    /// A `wl_surface.frame` callback fired. `[window_id]`
    FrameCallback,
    /// `wp_presentation_feedback.presented`.
    /// `[frame_id, presented_ns, refresh_ns, flags]`, `presented_ns` being on
    /// the clock announced by `PresentationClock`.
    Presented,
    /// `wp_presentation_feedback.discarded`. `[frame_id]`
    Discarded,
    /// `wp_presentation.clock_id`. `[clk_id]`
    PresentationClock,
}

impl Event {
    pub fn name(self) -> &'static str {
        match self {
            Self::WlKey => "wl_key",
            Self::PtyWrite => "pty_write",
            Self::PtyRead => "pty_read",
            Self::ParseApply => "parse_apply",
            Self::GuiOutput => "gui_output",
            Self::Invalidate => "invalidate",
            Self::PaintStart => "paint_start",
            Self::PaintPane => "paint_pane",
            Self::PaintEnd => "paint_end",
            Self::Commit => "commit",
            Self::FrameCallback => "frame_cb",
            Self::Presented => "presented",
            Self::Discarded => "discarded",
            Self::PresentationClock => "pres_clock",
        }
    }
}

/// Read `WEZTERM_LATENCY_TRACE` and remove it from the environment, keeping
/// the path for when the first record opens the trace. Call it at the top of
/// `main` while the process is still single-threaded: modifying the
/// environment races with `getenv` in other threads.
pub fn take_env() {
    let path = std::env::var_os(ENV_VAR);
    std::env::remove_var(ENV_VAR);
    let _ = TRACE_PATH.set(path);
}

/// The trace path, for passing the variable on to a process that should
/// write to the same trace (for example a re-exec of ourselves).
pub fn trace_path() -> Option<&'static std::ffi::OsStr> {
    TRACE_PATH
        .get_or_init(|| std::env::var_os(ENV_VAR))
        .as_deref()
}

/// The environment variable that enables tracing.
pub const ENV_VAR: &str = "WEZTERM_LATENCY_TRACE";
static TRACE_PATH: std::sync::OnceLock<Option<std::ffi::OsString>> = std::sync::OnceLock::new();

/// Whether tracing is enabled. Use it to skip computing values that are only
/// needed for a record.
#[inline]
pub fn enabled() -> bool {
    #[cfg(unix)]
    {
        imp::sink().is_some()
    }
    #[cfg(not(unix))]
    {
        false
    }
}

/// The current `CLOCK_MONOTONIC` time in nanoseconds; 0 where tracing is not
/// supported.
pub fn now_ns() -> u64 {
    #[cfg(unix)]
    {
        imp::now_ns()
    }
    #[cfg(not(unix))]
    {
        0
    }
}

/// Record `event` as happening now. See [`Event`] for the values each event
/// carries; at most 4 are kept.
#[inline]
pub fn record(event: Event, values: &[u64]) {
    #[cfg(unix)]
    if let Some(tx) = imp::sink() {
        imp::send(tx, imp::now_ns(), event, values);
    }
    #[cfg(not(unix))]
    let _ = (event, values);
}

/// Record `event` with an explicit timestamp, for events whose time is known
/// more precisely than when the code observes them.
#[inline]
pub fn record_at(ts_ns: u64, event: Event, values: &[u64]) {
    #[cfg(unix)]
    if let Some(tx) = imp::sink() {
        imp::send(tx, ts_ns, event, values);
    }
    #[cfg(not(unix))]
    let _ = (ts_ns, event, values);
}

#[cfg(unix)]
mod imp {
    use super::Event;
    use std::fs::{OpenOptions, Permissions};
    use std::io::{BufWriter, Write};
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    use std::sync::mpsc::{channel, RecvTimeoutError, Sender};
    use std::sync::OnceLock;
    use std::time::{Duration, Instant};

    const MAX_VALUES: usize = 4;

    /// Upper bound on how long a written record stays in the buffer.
    const FLUSH_INTERVAL: Duration = Duration::from_millis(50);

    pub(super) struct Record {
        ts_ns: u64,
        event: Event,
        values: [u64; MAX_VALUES],
        len: usize,
    }

    static SINK: OnceLock<Option<Sender<Record>>> = OnceLock::new();

    /// The fast path: once initialized, a single atomic load.
    #[inline]
    pub(super) fn sink() -> Option<&'static Sender<Record>> {
        match SINK.get() {
            Some(sink) => sink.as_ref(),
            None => init_sink(),
        }
    }

    #[cold]
    #[inline(never)]
    fn init_sink() -> Option<&'static Sender<Record>> {
        SINK.get_or_init(open_sink).as_ref()
    }

    fn open_sink() -> Option<Sender<Record>> {
        let path = super::trace_path()?;
        let file = match OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&path)
        {
            Ok(file) => file,
            Err(err) => {
                eprintln!("latency-trace: cannot create {:?}: {}", path, err);
                return None;
            }
        };
        // `mode` only applies when the file is created; tighten a regular
        // file that already existed too. Other targets, such as a fifo or a
        // tty, are left alone.
        if file.metadata().map(|m| m.is_file()).unwrap_or(false) {
            if let Err(err) = file.set_permissions(Permissions::from_mode(0o600)) {
                eprintln!(
                    "latency-trace: cannot restrict permissions of {:?}: {}",
                    path, err
                );
            }
        }
        let (tx, rx) = channel::<Record>();
        std::thread::Builder::new()
            .name("latency-trace".into())
            .spawn(move || {
                let mut out = BufWriter::new(file);
                let _ = writeln!(
                    out,
                    "# wezterm latency trace pid={} clock=CLOCK_MONOTONIC",
                    std::process::id()
                );
                // Set while the buffer holds records that are not flushed yet:
                // the time by which they must be.
                let mut flush_by: Option<Instant> = None;
                loop {
                    let rec = match flush_by {
                        Some(deadline) => {
                            match rx
                                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                            {
                                Ok(rec) => rec,
                                Err(RecvTimeoutError::Timeout) => {
                                    let _ = out.flush();
                                    flush_by = None;
                                    continue;
                                }
                                Err(RecvTimeoutError::Disconnected) => break,
                            }
                        }
                        None => match rx.recv() {
                            Ok(rec) => rec,
                            Err(_) => break,
                        },
                    };
                    let _ = write!(out, "{},{}", rec.ts_ns, rec.event.name());
                    for v in &rec.values[..rec.len] {
                        let _ = write!(out, ",{}", v);
                    }
                    let _ = out.write_all(b"\n");
                    let deadline = *flush_by.get_or_insert_with(|| Instant::now() + FLUSH_INTERVAL);
                    // Under sustained traffic the timeout never fires
                    if Instant::now() >= deadline {
                        let _ = out.flush();
                        flush_by = None;
                    }
                }
                let _ = out.flush();
            })
            .ok()?;
        Some(tx)
    }

    pub(super) fn now_ns() -> u64 {
        let mut ts = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        unsafe {
            libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts);
        }
        ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64
    }

    pub(super) fn send(tx: &Sender<Record>, ts_ns: u64, event: Event, values: &[u64]) {
        let len = values.len().min(MAX_VALUES);
        let mut buf = [0u64; MAX_VALUES];
        buf[..len].copy_from_slice(&values[..len]);
        let _ = tx.send(Record {
            ts_ns,
            event,
            values: buf,
            len,
        });
    }
}
