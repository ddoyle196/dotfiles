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
