# window:focus()

{{since('20230320-124340-559cb7b0')}}

Attempts to focus and activate the window.

|OS             |Supported?|
|---------------|------------------------|
|macOS          |Yes                     |
|Windows        |Yes                     |
|X11            |Yes                     |
|Wayland        |Yes*                    |

\* On Wayland the request goes through `xdg-activation-v1` and the compositor
decides. Requests that don't follow recent user input, or come from an
unfocused client, are commonly ignored, often with a "demands attention"
indication instead of a raise. Nothing happens if the compositor lacks the
protocol.
