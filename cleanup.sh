#!/usr/bin/env bash
set -euo pipefail

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
      echo "$APPDATA/nvim"
      ;;
    *)
      echo "$HOME/.config/nvim"
      ;;
  esac
}

remove_link() {
  local dest="$1"
  local latest_backup

  if [[ -L "$dest" ]]; then
    log "Removing symlink: $dest"
    rm "$dest"

    # Restore most recent backup if exists
    latest_backup=$(ls -t "${dest}.backup."* 2>/dev/null | head -1 || true)
    if [[ -n "$latest_backup" ]]; then
      log "Restoring backup: $latest_backup -> $dest"
      mv "$latest_backup" "$dest"
    fi
  elif [[ -e "$dest" ]]; then
    warn "Not a symlink, skipping: $dest"
  else
    log "Nothing to remove: $dest"
  fi
}

main() {
  local os
  os=$(detect_os)
  log "Detected OS: $os"
  log "Cleaning up dotfiles symlinks..."

  local nvim_config_dir
  nvim_config_dir=$(get_nvim_config_dir "$os")

  remove_link "$nvim_config_dir"

  if [[ "$os" != "windows-bash" ]]; then
    remove_link "$HOME/.tmux.conf"
  fi

  log "Cleanup complete!"
  log "Note: TPM plugins left in ~/.tmux/plugins (remove manually if desired)"
}

main "$@"
