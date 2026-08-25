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

# The Claude cockpit: prefix+a opens a list of your conversations beside the one
# you picked. Linked file by file, because ~/.tmux/scripts and ~/.claude/hooks
# hold other things too. Set COCKPIT_DIR to the directory the conversations
# should run in; it defaults to ~/Dev/project.
install_cockpit() {
  local f
  log "Linking Claude cockpit..."
  mkdir -p "$HOME/.tmux/scripts/lib" "$HOME/.claude/hooks" "$HOME/.local/bin"
  for f in cc-panel.sh cc-host.sh cc-stage.sh cc-keys.sh cc-cockpit.sh \
           cc-run-panel.sh cc-list.py cc-harvest.py; do
    backup_and_link "$DOTFILES_DIR/tmux/scripts/$f" "$HOME/.tmux/scripts/$f"
  done
  backup_and_link "$DOTFILES_DIR/tmux/scripts/lib/cc-usage-report.py" \
                  "$HOME/.tmux/scripts/lib/cc-usage-report.py"
  for f in cc-recap-gen.sh cc-recap-trigger.sh; do
    backup_and_link "$DOTFILES_DIR/claude/hooks/$f" "$HOME/.claude/hooks/$f"
  done
  [[ -f "$DOTFILES_DIR/bin/cockpit" ]] &&
    backup_and_link "$DOTFILES_DIR/bin/cockpit" "$HOME/.local/bin/cockpit"

  register_recap_hook

  for f in jq python3 tmux claude; do
    command -v "$f" &>/dev/null || warn "cockpit needs $f on PATH"
  done
}

# The recap hook is what keeps the list's one-line summaries current. Linking the
# script is not enough - it has to be registered as a Stop hook, and that lives
# in settings.json alongside machine-specific things we do not want to sync.
# Added in place, and only when it is not already there.
register_recap_hook() {
  local settings="$HOME/.claude/settings.json"
  local cmd='$HOME/.claude/hooks/cc-recap-trigger.sh'

  command -v jq &>/dev/null || { warn "jq not found, skipping recap hook"; return; }
  [[ -f "$settings" ]] || echo '{}' > "$settings"

  if jq -e --arg c "$cmd" \
       '[.hooks.Stop[]?.hooks[]?.command] | index($c)' "$settings" >/dev/null 2>&1; then
    log "Recap hook already registered"
    return
  fi

  log "Registering recap hook in settings.json"
  jq --arg c "$cmd" \
     '.hooks.Stop = ((.hooks.Stop // []) + [{hooks:[{type:"command", command:$c}]}])' \
     "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
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
    install_cockpit
    install_tpm
  fi

  log "Setup complete!"
}

main "$@"
