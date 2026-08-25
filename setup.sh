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
# should run in; setup records it per machine so no shell rc needs editing.
install_cockpit() {
  local f
  log "Linking Claude cockpit..."
  mkdir -p "$HOME/.tmux/scripts/lib" "$HOME/.claude/hooks" "$HOME/.local/bin"
  # save/restore-labels are not cockpit files, but tmux.conf points its resurrect
  # hooks at them, so they have to land in the same place.
  for f in cc-panel.sh cc-host.sh cc-stage.sh cc-keys.sh cc-cockpit.sh \
           cc-run-panel.sh cc-list.py cc-harvest.py \
           save-labels.sh restore-labels.sh; do
    backup_and_link "$DOTFILES_DIR/tmux/scripts/$f" "$HOME/.tmux/scripts/$f"
  done
  # Everything in lib/, not a named list: cc-dir.sh was added later and a fresh
  # install had no way to resolve its cockpit directory without it.
  for f in "$DOTFILES_DIR"/tmux/scripts/lib/*; do
    backup_and_link "$f" "$HOME/.tmux/scripts/lib/$(basename "$f")"
  done
  for f in cc-recap-gen.sh cc-recap-trigger.sh nested-claude-md.py; do
    backup_and_link "$DOTFILES_DIR/claude/hooks/$f" "$HOME/.claude/hooks/$f"
  done
  [[ -f "$DOTFILES_DIR/bin/cockpit" ]] &&
    backup_and_link "$DOTFILES_DIR/bin/cockpit" "$HOME/.local/bin/cockpit"

  record_cockpit_dir
  register_recap_hook
  register_nested_md_hook

  for f in jq python3 tmux claude; do
    command -v "$f" &>/dev/null || warn "cockpit needs $f on PATH"
  done
}

# Which directory new conversations start in. Recorded in a file the scripts
# read, not exported into a shell rc: the tmux server carries the environment of
# whatever launched it, which is rarely the shell you are typing in. Set once,
# and never guessed - an unset COCKPIT_DIR on a fresh machine means $HOME, which
# is harmless, rather than a path from somebody else's laptop.
record_cockpit_dir() {
  local f="$HOME/.claude/cockpit/dir" dir="${COCKPIT_DIR:-}"
  mkdir -p "$HOME/.claude/cockpit" "$HOME/.claude/session-index"
  if [[ -n "$dir" ]]; then
    :
  elif [[ -s "$f" ]]; then
    log "Cockpit directory already set: $(cat "$f")"
    return
  elif [[ -t 0 ]]; then
    read -r "dir?Directory for new cockpit conversations [$HOME]: "
  fi
  dir="${dir:-$HOME}"
  eval dir="$dir"                       # let a typed ~ or $HOME expand
  [[ -d "$dir" ]] || warn "cockpit directory does not exist yet: $dir"
  printf '%s\n' "$dir" > "$f"
  log "Cockpit directory: $dir"
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

# Loads a subdirectory's CLAUDE.md when work reaches that subdirectory, which
# Claude Code does not do on its own. Bash is in the matcher deliberately: under
# --dangerously-skip-permissions, which is how the cockpit runs, the agent reads
# files with `cat` far more often than with the Read tool.
register_nested_md_hook() {
  local settings="$HOME/.claude/settings.json"
  local cmd="$HOME/.claude/hooks/nested-claude-md.py"

  command -v jq &>/dev/null || { warn "jq not found, skipping nested CLAUDE.md hook"; return; }
  [[ -f "$settings" ]] || echo '{}' > "$settings"

  if jq -e --arg c "$cmd" \
       '[.hooks.PreToolUse[]?.hooks[]?.command] | index($c)' "$settings" >/dev/null 2>&1; then
    log "Nested CLAUDE.md hook already registered"
    return
  fi

  log "Registering nested CLAUDE.md hook in settings.json"
  jq --arg c "$cmd" \
     '.hooks.PreToolUse = ((.hooks.PreToolUse // []) + [{
        matcher: "Bash|Read|Edit|Write|MultiEdit|NotebookEdit|Grep|Glob",
        hooks: [{type: "command", command: $c}]
      }])' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
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
    # A running server is still on the old config; without this the new
    # bindings only appear after a restart, which reads as setup not working.
    if tmux info &>/dev/null; then
      tmux source-file "$HOME/.tmux.conf" 2>/dev/null &&
        log "Reloaded tmux config" || warn "Could not reload tmux config"
    fi
  fi

  log "Setup complete!"
}

main "$@"
