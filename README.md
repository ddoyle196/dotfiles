# Dotfiles

Personal configuration for tmux and neovim. Cross-platform support for macOS, Linux, and Windows.

## Quick Start

```bash
git clone git@github.com:YOUR_USERNAME/dotfiles.git ~/dotfiles
cd ~/dotfiles
./setup.sh        # macOS / Linux / WSL / Git Bash
```

**Windows (PowerShell):**
```powershell
git clone git@github.com:YOUR_USERNAME/dotfiles.git $HOME\dotfiles
cd $HOME\dotfiles
.\setup.ps1
```

## Platform Support

| Platform | Neovim | Tmux | Script |
|----------|--------|------|--------|
| macOS | ✅ | ✅ | `setup.sh` |
| Linux | ✅ | ✅ | `setup.sh` |
| WSL | ✅ | ✅ | `setup.sh` |
| Windows (Git Bash) | ✅ | ❌ | `setup.sh` |
| Windows (PowerShell) | ✅ | ❌ | `setup.ps1` |

> **Note:** Tmux doesn't run natively on Windows. Use WSL for full tmux support.

## Config Locations

| Platform | Neovim | Tmux |
|----------|--------|------|
| macOS/Linux/WSL | `~/.config/nvim` | `~/.tmux.conf` |
| Windows | `%LOCALAPPDATA%\nvim` | N/A |

## Windows Requirements

For symlinks on Windows, you need **one** of:
- **Developer Mode** enabled (Settings → Privacy & Security → For developers)
- Run PowerShell/Terminal as **Administrator**

## Cleanup

Remove symlinks and restore original configs:

```bash
./cleanup.sh        # macOS / Linux / WSL / Git Bash
```

```powershell
.\cleanup.ps1       # Windows PowerShell
```

## Structure

```
dotfiles/
├── nvim/           # Neovim config (LazyVim)
├── tmux.conf       # Tmux config
├── setup.sh        # Unix setup script
├── setup.ps1       # Windows PowerShell setup
├── cleanup.sh      # Unix cleanup script
└── cleanup.ps1     # Windows PowerShell cleanup
```

## Post-Setup

- **Tmux**: Press `prefix + I` to install plugins via TPM
- **Neovim**: Plugins auto-install on first launch via lazy.nvim

## Claude cockpit

A list of your Claude Code conversations on the left, the one you picked on the
right. `prefix + a` opens it; the list follows your cursor, so moving down the
list swaps the conversation beside it.

A conversation is a registry file under `~/.claude/cockpit/`, not a process. The
tmux session is only how it happens to be running right now — when that dies the
entry stays, and `enter` resumes it from its transcript.

| | |
|---|---|
| `enter` | open it (resumes a parked one) |
| `a` | reply without leaving the list |
| `/` | fuzzy jump |
| `t` / `n` | new topic / new conversation |
| `x` / `d` | park (stop the process) / remove |
| `u` | tokens and time per topic |
| `?` | the full legend lives in the status bar |

Rows show what each conversation needs from you (`answer`, `pick up`, `waiting`,
`done`), whether it has spoken since you last looked, and any pull requests it
has worked on.

Set `COCKPIT_DIR` to the directory conversations should run in. It defaults to
`~/Dev/project`, so on another machine export it somewhere your shell reads:

```sh
export COCKPIT_DIR="$HOME/code"
```

Needs `tmux`, `jq`, `python3` and the `claude` CLI. `gh` is optional and only
used for pull-request badges. Nothing about the cockpit is synced except the
code: the conversations themselves are per-machine, so it starts empty.
