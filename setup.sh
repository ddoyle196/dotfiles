#!/usr/bin/env bash
set -euo pipefail

DOTFILES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[dotfiles] $*"; }
warn() { echo "[dotfiles] WARNING: $*" >&2; }
error() { echo "[dotfiles] ERROR: $*" >&2; exit 1; }

detect_os() {
  case "$(uname -s)" in
    Linux*)
      if grep -qEi "(Microsoft|WSL)" /proc/version 2>/dev/null; then
        echo "wsl"
      else
        echo "linux"
      fi
      ;;
    Darwin*) echo "macos" ;;
    CYGWIN*|MINGW*|MSYS*) echo "windows-bash" ;;
    *) error "Unsupported OS: $(uname -s)" ;;
  esac
}

get_nvim_config_dir() {
  local os="$1"
  case "$os" in
    windows-bash)
      # Git Bash / MSYS2 on Windows
      echo "$APPDATA/nvim"
      ;;
    *)
      echo "$HOME/.config/nvim"
      ;;
  esac
}

backup_and_link() {
  local src="$1"
  local dest="$2"
  local backup="${dest}.backup.$(date +%Y%m%d%H%M%S)"

  # Ensure parent directory exists
  mkdir -p "$(dirname "$dest")"

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

install_tpm() {
  local tpm_dir="$HOME/.tmux/plugins/tpm"
  if [[ ! -d "$tpm_dir" ]]; then
    log "Installing tmux plugin manager (TPM)..."
    git clone https://github.com/tmux-plugins/tpm "$tpm_dir"
    log "TPM installed. Press prefix + I in tmux to install plugins."
  else
    log "TPM already installed"
  fi
}

main() {
  local os
  os=$(detect_os)
  log "Detected OS: $os"
  log "Setting up dotfiles from $DOTFILES_DIR"

  local nvim_config_dir
  nvim_config_dir=$(get_nvim_config_dir "$os")

  # Neovim (all platforms)
  backup_and_link "$DOTFILES_DIR/nvim" "$nvim_config_dir"

  # Tmux (skip on native Windows - use WSL for tmux)
  if [[ "$os" == "windows-bash" ]]; then
    warn "Skipping tmux on native Windows. Use WSL for tmux support."
  else
    backup_and_link "$DOTFILES_DIR/tmux.conf" "$HOME/.tmux.conf"
    install_tpm
  fi

  log "Setup complete!"
}

main "$@"
