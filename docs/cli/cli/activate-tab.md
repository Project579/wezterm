# `wezterm cli activate-tab`

{{since('20230326-111934-3666303c')}}

## Synopsis

```console
{% include "../../examples/cmd-synopsis-wezterm-cli-activate-tab--help.txt" %}
```

On Wayland a client cannot raise itself unless the compositor grants it an
activation token. `--activation-token` spends a token obtained elsewhere,
typically by a notification handler that received one when the user clicked a
notification action. If the flag is omitted, `$XDG_ACTIVATION_TOKEN` is used
when it is set. Tokens are single-use and short-lived, so spend one
immediately. On other platforms the option is accepted and ignored.

