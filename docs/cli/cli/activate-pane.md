# `wezterm cli activate-pane`

{{since('20230326-111934-3666303c')}}

Activates the current pane, or the pane specified via the `--pane-id`
parameter.

## Synopsis

```console
{% include "../../examples/cmd-synopsis-wezterm-cli-activate-pane--help.txt" %}
```

On Wayland a client cannot raise itself unless the compositor grants it an
activation token. `--activation-token` spends a token obtained elsewhere,
typically by a notification handler that received one when the user clicked a
notification action. If the flag is omitted, `$XDG_ACTIVATION_TOKEN` is used
when it is set. Tokens are single-use and short-lived, so spend one
immediately. On other platforms the option is accepted and ignored.
