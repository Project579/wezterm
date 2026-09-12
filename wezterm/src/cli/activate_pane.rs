use clap::Parser;
use mux::pane::PaneId;
use wezterm_client::client::Client;

#[derive(Debug, Parser, Clone)]
pub struct ActivatePane {
    /// Specify the target pane.
    /// The default is to use the current pane based on the
    /// environment variable WEZTERM_PANE.
    #[arg(long)]
    pane_id: Option<PaneId>,

    /// Raise the containing window using this xdg-activation token.
    /// Falls back to $XDG_ACTIVATION_TOKEN. Wayland only; ignored elsewhere.
    #[arg(long)]
    activation_token: Option<String>,
}

impl ActivatePane {
    pub async fn run(&self, client: Client) -> anyhow::Result<()> {
        let pane_id = client.resolve_pane_id(self.pane_id).await?;
        client
            .set_focused_pane_id(codec::SetFocusedPane {
                pane_id,
                activation_token: crate::cli::resolve_activation_token(
                    self.activation_token.as_deref(),
                ),
            })
            .await?;
        Ok(())
    }
}
