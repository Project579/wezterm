# `wezterm cli activate-pane`

{{since('20230326-111934-3666303c')}}

Activates the current pane, or the pane specified via the `--pane-id`
parameter.

## Synopsis

```console
{% include "../../examples/cmd-synopsis-wezterm-cli-activate-pane--help.txt" %}
```

On Wayland a client cannot raise itself without a compositor-granted
activation token. `--activation-token` spends one obtained elsewhere, usually
by a notification handler whose action the user clicked. Without the flag,
`$XDG_ACTIVATION_TOKEN` is used if set. Tokens are single-use and short-lived,
so spend one immediately. Ignored on other platforms.
