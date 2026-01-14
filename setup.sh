#!/usr/bin/env bash
set -euo pipefail

DOTFILES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[dotfiles] $*"; }
warn() { echo "[dotfiles] WARNING: $*" >&2; }

backup_and_link() {
  local src="$1"
  local dest="$2"
  local backup="${dest}.backup.$(date +%Y%m%d%H%M%S)"

  if [[ -L "$dest" ]]; then
    log "Removing existing symlink: $dest"
    rm "$dest"
  elif [[ -e "$dest" ]]; then
    log "Backing up existing: $dest -> $backup"
    mv "$dest" "$backup"
  fi

  log "Linking: $dest -> $src"
  ln -s "$src" "$dest"
}

main() {
  log "Setting up dotfiles from $DOTFILES_DIR"

  # Ensure ~/.config exists
  mkdir -p "$HOME/.config"

  # Tmux
  backup_and_link "$DOTFILES_DIR/tmux.conf" "$HOME/.tmux.conf"

  # Neovim
  backup_and_link "$DOTFILES_DIR/nvim" "$HOME/.config/nvim"

  # Install tmux plugin manager if not present
  if [[ ! -d "$HOME/.tmux/plugins/tpm" ]]; then
    log "Installing tmux plugin manager (TPM)..."
    git clone https://github.com/tmux-plugins/tpm "$HOME/.tmux/plugins/tpm"
    log "TPM installed. Press prefix + I in tmux to install plugins."
  fi

  log "Setup complete!"
}

main "$@"
