# `wezterm cli activate-tab`

{{since('20230326-111934-3666303c')}}

## Synopsis

```console
{% include "../../examples/cmd-synopsis-wezterm-cli-activate-tab--help.txt" %}
```

On Wayland a client cannot raise itself without a compositor-granted
activation token. `--activation-token` spends one obtained elsewhere, usually
by a notification handler whose action the user clicked. Without the flag,
`$XDG_ACTIVATION_TOKEN` is used if set. Tokens are single-use and short-lived,
so spend one immediately. Ignored on other platforms.
