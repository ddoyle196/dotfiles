#!/usr/bin/env bash
set -euo pipefail

log() { echo "[dotfiles] $*"; }
warn() { echo "[dotfiles] WARNING: $*" >&2; }

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
  log "Cleaning up dotfiles symlinks..."

  remove_link "$HOME/.tmux.conf"
  remove_link "$HOME/.config/nvim"

  log "Cleanup complete!"
  log "Note: TPM plugins left in ~/.tmux/plugins (remove manually if desired)"
}

main "$@"
