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

install_packages() {
  local os="$1"
  shift
  local packages=("$@")

  log "Installing packages: ${packages[*]}"

  case "$os" in
    macos)
      if ! command -v brew &>/dev/null; then
        error "Homebrew not found. Install from https://brew.sh"
      fi
      brew install "${packages[@]}"
      ;;
    linux|wsl)
      if command -v apt-get &>/dev/null; then
        sudo apt-get update
        sudo apt-get install -y "${packages[@]}"
      elif command -v dnf &>/dev/null; then
        sudo dnf install -y "${packages[@]}"
      elif command -v pacman &>/dev/null; then
        sudo pacman -Sy --noconfirm "${packages[@]}"
      else
        error "No supported package manager found (apt, dnf, pacman)"
      fi
      ;;
    *)
      error "Cannot install packages on $os"
      ;;
  esac
}

ensure_installed() {
  local os="$1"
  local cmd="$2"
  local pkg="${3:-$cmd}"  # package name, defaults to command name

  if ! command -v "$cmd" &>/dev/null; then
    log "$cmd not found, installing..."
    install_packages "$os" "$pkg"
  else
    log "$cmd already installed"
  fi
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

backup_and_link() {
  local src="$1"
  local dest="$2"
  local backup="${dest}.backup.$(date +%Y%m%d%H%M%S)"

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

install_nvim_plugins() {
  log "Installing neovim plugins..."
  nvim --headless "+Lazy! sync" +qa
}

install_tpm() {
  local tpm_dir="$HOME/.tmux/plugins/tpm"
  if [[ ! -d "$tpm_dir" ]]; then
    log "Installing tmux plugin manager (TPM)..."
    git clone https://github.com/tmux-plugins/tpm "$tpm_dir"
  else
    log "TPM already installed"
  fi

  log "Installing tmux plugins..."
  "$tpm_dir/bin/install_plugins"
}

main() {
  local os
  os=$(detect_os)
  log "Detected OS: $os"
  log "Setting up dotfiles from $DOTFILES_DIR"

  # Install dependencies
  ensure_installed "$os" "nvim" "neovim"
  if [[ "$os" != "windows-bash" ]]; then
    ensure_installed "$os" "tmux"
  fi

  # Neovim config
  local nvim_config_dir
  nvim_config_dir=$(get_nvim_config_dir "$os")
  backup_and_link "$DOTFILES_DIR/nvim" "$nvim_config_dir"
  install_nvim_plugins

  # Tmux config (skip on native Windows)
  if [[ "$os" == "windows-bash" ]]; then
    warn "Skipping tmux on native Windows. Use WSL for tmux support."
  else
    backup_and_link "$DOTFILES_DIR/tmux.conf" "$HOME/.tmux.conf"
    install_tpm
  fi

  log "Setup complete!"
}

main "$@"
