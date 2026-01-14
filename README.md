# Dotfiles

Personal configuration for tmux and neovim.

## Setup

```bash
git clone git@github.com:YOUR_USERNAME/dotfiles.git ~/dotfiles
cd ~/dotfiles
./setup.sh
```

This will:
- Back up any existing configs (with timestamp)
- Create symlinks to the repo files
- Install tmux plugin manager (TPM) if not present

## Cleanup

```bash
./cleanup.sh
```

Removes symlinks and restores the most recent backup if available.

## Structure

```
dotfiles/
├── nvim/           # Neovim config (LazyVim)
├── tmux.conf       # Tmux config
├── setup.sh        # Installation script
└── cleanup.sh      # Removal script
```

## Post-Setup

- **Tmux**: Press `prefix + I` to install plugins via TPM
- **Neovim**: Plugins auto-install on first launch via lazy.nvim
