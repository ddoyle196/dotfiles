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

# One-time move from the old name. Safe to re-run: every step checks first.
migrate_from_cockpit() {
  local old="$HOME/.claude/cockpit" new="$HOME/.claude/helm" l f
  local plist="$HOME/Library/LaunchAgents/com.ddoyle.cockpit-web.plist"
  local settings="$HOME/.claude/settings.json"

  if [[ -d "$old" && ! -L "$old" ]]; then
    if [[ -e "$new" && -z $(ls -A "$new/threads" 2>/dev/null) ]]; then
      # Renamed code recreates an empty dir on first run; the old one holds the real data.
      local aside="$new.pre-migrate.$(date +%Y%m%d%H%M%S)"
      log "Setting aside empty $new -> $aside"
      mv "$new" "$aside"
    fi
    if [[ -e "$new" ]]; then
      warn "Both $old and $new have threads: leaving both, merge by hand"
    else
      log "Moving $old -> $new"
      mv "$old" "$new"
      for f in "" -wal -shm; do
        [[ -e "$new/cockpit.db$f" && ! -e "$new/helm.db$f" ]] &&
          mv "$new/cockpit.db$f" "$new/helm.db$f"
      done
    fi
  fi

  for l in "$HOME/.tmux/scripts/cc-cockpit.sh" "$HOME/.tmux/scripts/cockpit-web" \
           "$HOME/.claude/hooks/cockpit-branch.py" "$HOME/.local/bin/cockpit"; do
    [[ -L "$l" ]] && rm "$l"
  done

  if [[ -e "$plist" ]]; then
    launchctl bootout "gui/$(id -u)/com.ddoyle.cockpit-web" 2>/dev/null || true
    rm -f "$plist"
  fi

  if [[ -f "$settings" ]] && command -v jq &>/dev/null &&
     grep -q 'hooks/cockpit-branch.py' "$settings"; then
    cp "$settings" "$settings.backup.$(date +%Y%m%d%H%M%S)"
    jq 'walk(if type == "string" and . == "$HOME/.claude/hooks/cockpit-branch.py"
              then "$HOME/.claude/hooks/helm-branch.py" else . end)' \
       "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
  fi
}

# Claude helm: prefix+a opens a list of your conversations beside the one
# you picked. Linked file by file, because ~/.tmux/scripts and ~/.claude/hooks
# hold other things too. Set HELM_DIR to the directory the conversations
# should run in; setup records it per machine so no shell rc needs editing.
install_helm() {
  local f
  log "Linking Claude helm..."
  migrate_from_cockpit
  mkdir -p "$HOME/.tmux/scripts/lib" "$HOME/.claude/hooks" "$HOME/.local/bin"
  # save/restore-labels are not helm files, but tmux.conf points its resurrect
  # hooks at them, so they have to land in the same place.
  for f in cc-panel.sh cc-host.sh cc-stage.sh cc-keys.sh cc-helm.sh \
           cc-run-panel.sh cc-list.py cc-harvest.py cc-search.py \
           save-labels.sh restore-labels.sh; do
    backup_and_link "$DOTFILES_DIR/tmux/scripts/$f" "$HOME/.tmux/scripts/$f"
  done
  # Everything in lib/, not a named list: cc-dir.sh was added later and a fresh
  # install had no way to resolve its helm directory without it.
  for f in "$DOTFILES_DIR"/tmux/scripts/lib/*; do
    backup_and_link "$f" "$HOME/.tmux/scripts/lib/$(basename "$f")"
  done
  for f in cc-recap-gen.sh cc-recap-trigger.sh nested-claude-md.py chrome-browser-pin.py \
           helm-branch.py lane-context.py; do
    backup_and_link "$DOTFILES_DIR/claude/hooks/$f" "$HOME/.claude/hooks/$f"
  done
  [[ -f "$DOTFILES_DIR/bin/helm" ]] &&
    backup_and_link "$DOTFILES_DIR/bin/helm" "$HOME/.local/bin/helm"
  # Linked as a directory, like helm-web: its files find each other by
  # sibling path. `lane` goes on PATH because the main chat is told to run it.
  if [[ -d "$DOTFILES_DIR/tmux/scripts/lanes" ]]; then
    backup_and_link "$DOTFILES_DIR/tmux/scripts/lanes" "$HOME/.tmux/scripts/lanes"
    [[ -f "$DOTFILES_DIR/tmux/scripts/lanes/lane" ]] &&
      backup_and_link "$DOTFILES_DIR/tmux/scripts/lanes/lane" "$HOME/.local/bin/lane"
  fi
  if [[ -d "$DOTFILES_DIR/helm-web" ]]; then
    backup_and_link "$DOTFILES_DIR/helm-web" "$HOME/.tmux/scripts/helm-web"
    install_helm_web_agent
  fi

  record_helm_dir
  record_pr_owners
  register_recap_hook
  register_nested_md_hook
  register_chrome_pin_hook
  register_helm_branch_hook
  register_lane_context_hook

  for f in jq python3 tmux claude; do
    command -v "$f" &>/dev/null || warn "helm needs $f on PATH"
  done
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "add \$HOME/.local/bin to PATH so 'helm' and 'lane' resolve" ;;
  esac
}

# Browser helm at http://localhost:7477, kept running by a login agent so
# the page is simply there. launchd starts agents with a bare system PATH, and
# the server shells out to tmux, jq, zsh and claude, so the PATH is built from
# wherever this machine keeps them.
install_helm_web_agent() {
  if [[ "$(uname -s)" != Darwin ]]; then
    log "No login agent here: run 'helm web' to start helm in the browser"
    return
  fi
  local label="com.ddoyle.helm-web" py c d path="/usr/bin:/bin:/usr/sbin:/sbin"
  local plist="$HOME/Library/LaunchAgents/$label.plist" domain="gui/$(id -u)"
  # The real interpreter, not a pyenv shim: a shim follows whatever version is
  # global at the time, so the agent would change under a later pyenv switch.
  if ! py=$(python3 -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null); then
    warn "python3 not found: web helm agent not installed"
    return
  fi
  for c in python3 tmux jq zsh claude gh; do
    d=$(dirname "$(command -v "$c" 2>/dev/null || echo /usr/bin/x)")
    [[ ":$path:" == *":$d:"* ]] || path="$d:$path"
  done
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$py</string>
    <string>$HOME/.tmux/scripts/helm-web/server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$path</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/helm-web.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/helm-web.log</string>
</dict>
</plist>
EOF
  # bootstrap refuses a label that is already loaded, so a re-run of setup has
  # to unload the old one first to pick up a changed plist.
  # bootout returns before the label is gone, and bootstrapping it meanwhile
  # fails with error 5.
  if launchctl bootout "$domain/$label" 2>/dev/null; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      launchctl print "$domain/$label" >/dev/null 2>&1 || break
      sleep 0.2
    done
  fi
  if launchctl bootstrap "$domain" "$plist"; then
    log "Web helm agent loaded: http://localhost:7477"
  else
    warn "Could not load $plist; 'helm web' starts the server by hand"
  fi
}

# Which directory new conversations start in. Recorded in a file the scripts
# read, not exported into a shell rc: the tmux server carries the environment of
# whatever launched it, which is rarely the shell you are typing in. Set once,
# and never guessed - an unset HELM_DIR on a fresh machine means $HOME, which
# is harmless, rather than a path from somebody else's laptop.
# Which GitHub owners the PR badges poll. Same file-not-rc reasoning as the
# directory above. The suggested default is your login plus every org you are
# in, which is where your own PRs live; empty means poll everything.
record_pr_owners() {
  local f="$HOME/.claude/helm/pr-owners" owners="${HELM_PR_OWNERS:-}" guess=""
  mkdir -p "$HOME/.claude/helm"
  if [[ -n "$owners" ]]; then
    :
  elif [[ -s "$f" ]]; then
    log "PR badge owners already set: $(tr '\n' ' ' < "$f")"
    return
  elif [[ -t 0 ]]; then
    if command -v gh &>/dev/null; then
      guess=$( { gh api user -q .login; gh api user/orgs -q '.[].login'; } 2>/dev/null | tr '\n' ' ')
    fi
    read -r -p "GitHub owners whose PRs to badge [${guess:-all}]: " owners
    owners="${owners:-$guess}"
  fi
  printf '%s\n' $owners > "$f"
  log "PR badge owners: ${owners:-all}"
}

record_helm_dir() {
  local f="$HOME/.claude/helm/dir" dir="${HELM_DIR:-}"
  mkdir -p "$HOME/.claude/helm" "$HOME/.claude/session-index"
  if [[ -n "$dir" ]]; then
    :
  elif [[ -s "$f" ]]; then
    log "Helm directory already set: $(cat "$f")"
    return
  elif [[ -t 0 ]]; then
    read -r -p "Directory for new helm conversations [$HOME]: " dir
  fi
  dir="${dir:-$HOME}"
  eval dir="$dir"                       # let a typed ~ or $HOME expand
  [[ -d "$dir" ]] || warn "helm directory does not exist yet: $dir"
  printf '%s\n' "$dir" > "$f"
  log "Helm directory: $dir"
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
# --dangerously-skip-permissions, which is how the helm runs, the agent reads
# files with `cat` far more often than with the Read tool.
register_nested_md_hook() {
  local settings="$HOME/.claude/settings.json"
  # Literal $HOME, matching register_recap_hook: settings.json expands it, and
  # writing the expanded path instead makes the check below miss an existing
  # registration and append a duplicate on every run.
  local cmd='$HOME/.claude/hooks/nested-claude-md.py'

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

# Every machine shares one Claude account, so Claude in Chrome can reach any
# machine's browser and defaults to whichever was used last. This keeps each
# machine's sessions on its own Chrome: every browser tool is denied until
# select_browser names the deviceId found in this machine's extension storage.
# PostToolUse records that the pick succeeded, which is what unlocks the rest.
register_chrome_pin_hook() {
  local settings="$HOME/.claude/settings.json"
  local cmd='$HOME/.claude/hooks/chrome-browser-pin.py'

  command -v jq &>/dev/null || { warn "jq not found, skipping Chrome pin hook"; return; }
  [[ -f "$settings" ]] || echo '{}' > "$settings"

  if jq -e --arg c "$cmd" \
       '[.hooks.PreToolUse[]?.hooks[]?.command] | index($c)' "$settings" >/dev/null 2>&1; then
    log "Chrome pin hook already registered"
    return
  fi

  log "Registering Chrome pin hook in settings.json"
  jq --arg c "$cmd" \
     '.hooks.PreToolUse = ((.hooks.PreToolUse // []) + [{
        matcher: "mcp__claude-in-chrome__.*",
        hooks: [{type: "command", command: $c}]
      }])
      | .hooks.PostToolUse = ((.hooks.PostToolUse // []) + [{
        matcher: "mcp__claude-in-chrome__select_browser",
        hooks: [{type: "command", command: $c}]
      }])' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
}

# `/branch` copies the conversation and moves you into the copy, in the same pane,
# and helm follows that move on its own. Without this the conversation you
# branched away from drops off the list; with it, the pane becomes the branch and
# the original is registered as a parked row beside it. "fork" is the SessionStart
# source `/branch` resumes under.
register_helm_branch_hook() {
  local settings="$HOME/.claude/settings.json"
  local cmd='$HOME/.claude/hooks/helm-branch.py'

  command -v jq &>/dev/null || { warn "jq not found, skipping helm branch hook"; return; }
  [[ -f "$settings" ]] || echo '{}' > "$settings"

  if jq -e --arg c "$cmd" \
       '[.hooks.SessionStart[]?.hooks[]?.command] | index($c)' "$settings" >/dev/null 2>&1; then
    log "Helm branch hook already registered"
    return
  fi

  log "Registering helm branch hook in settings.json"
  jq --arg c "$cmd" \
     '.hooks.SessionStart = ((.hooks.SessionStart // []) + [{
        matcher: "fork",
        hooks: [{type: "command", command: $c}]
      }])' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
}

# The lanes main chat learns which lane is selected, and how to file work, from
# this hook. It exits at once unless CC_LANES_MAIN=1, so registering it for every
# conversation costs the others nothing.
register_lane_context_hook() {
  local settings="$HOME/.claude/settings.json"
  local cmd='python3 ~/.claude/hooks/lane-context.py' event

  command -v jq &>/dev/null || { warn "jq not found, skipping lane context hook"; return; }
  [[ -f "$settings" ]] || echo '{}' > "$settings"

  for event in SessionStart UserPromptSubmit; do
    if jq -e --arg c "$cmd" --arg e "$event" \
         '[.hooks[$e][]?.hooks[]?.command] | index($c)' "$settings" >/dev/null 2>&1; then
      log "Lane context hook already registered for $event"
      continue
    fi
    log "Registering lane context hook for $event in settings.json"
    jq --arg c "$cmd" --arg e "$event" \
       '.hooks[$e] = ((.hooks[$e] // []) + [{
          hooks: [{type: "command", command: $c, timeout: 5}]
        }])' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
  done
}

# OpenLogi drives the MX Master 4 in place of Logi Options+. Linked file by file,
# because ~/.config/openlogi also holds the agent's socket, locks and backups.
install_openlogi() {
  local os="$1" dir="$HOME/.config/openlogi"

  case "$os" in
    macos)
      if [[ -d /Applications/OpenLogi.app ]]; then
        log "OpenLogi already installed"
      else
        log "Installing OpenLogi..."
        # The aprilnea tap follows GitHub releases. The official cask lags, and
        # the version it held at first install could not open the Actions Ring.
        brew install --cask aprilnea/tap/openlogi@latest
      fi
      ;;
    linux)
      command -v openlogi &>/dev/null ||
        warn "OpenLogi not installed: https://github.com/AprilNEA/OpenLogi#linux"
      ;;
    *)
      return
      ;;
  esac

  backup_and_link "$DOTFILES_DIR/openlogi/config.toml" "$dir/config.toml"
  backup_and_link "$DOTFILES_DIR/openlogi/scripts/screenshot-hovered-window.sh" \
    "$dir/scripts/screenshot-hovered-window.sh"

  if [[ "$os" == "macos" ]]; then
    build_openlogi_helper "$dir/bin" thumbwheel-key
    build_openlogi_helper "$dir/bin" fn-key
    disable_logi_options_plus
  fi
}

# Compiled per machine rather than committed: a binary built elsewhere would be
# quarantined and is tied to the architecture it was built on.
build_openlogi_helper() {
  local name="$2" out="$1/$2"
  command -v clang &>/dev/null ||
    { warn "clang not found (xcode-select --install); $name will do nothing"; return; }
  mkdir -p "$1"
  log "Building OpenLogi helper: $out"
  clang -O2 -framework ApplicationServices -o "$out" \
    "$DOTFILES_DIR/openlogi/scripts/$name.c" ||
    warn "Could not build $name"
}

# Logi Options+ and OpenLogi fight over the mouse's HID++ channel, and Logi's
# launchd jobs restart it the moment it is quit, so turning it off means
# disabling those jobs. Left installed: `launchctl enable` on both brings it back.
disable_logi_options_plus() {
  local agent="gui/$(id -u)/com.logi.cp-dev-mgr"
  local updater="system/com.logi.optionsplus.updater"

  [[ -f /Library/LaunchAgents/com.logi.optionsplus.plist ]] || return 0

  log "Disabling Logi Options+ so OpenLogi can own the mouse"
  osascript -e 'quit app "logioptionsplus"' &>/dev/null || true
  launchctl disable "$agent"
  launchctl bootout "$agent" &>/dev/null || true

  if [[ -t 0 ]]; then
    sudo launchctl disable "$updater"
    sudo launchctl bootout "$updater" &>/dev/null || true
  else
    warn "Run: sudo launchctl disable $updater && sudo launchctl bootout $updater"
  fi
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
    install_helm
    install_openlogi "$os"
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
