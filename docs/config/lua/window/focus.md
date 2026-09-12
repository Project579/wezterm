# window:focus()

{{since('20230320-124340-559cb7b0')}}

Attempts to focus and activate the window.

|OS             |Supported?|
|---------------|------------------------|
|macOS          |Yes                     |
|Windows        |Yes                     |
|X11            |Yes                     |
|Wayland        |Yes*                    |


\* On Wayland a client cannot focus itself directly; the request is made through
the `xdg-activation-v1` protocol and the compositor decides whether to honor
it. Compositors commonly ignore activation requests that don't originate from
recent user input, or that come from a client that isn't currently focused, and
may show a "demands attention" indication rather than raising the window. The
request is most likely to be honored when another WezTerm window already has
focus. If the compositor doesn't implement `xdg-activation-v1` at all, this
does nothing.
