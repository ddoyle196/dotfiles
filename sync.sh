#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# Check for local changes
if [[ -n $(git status --porcelain) ]]; then
  echo "[dotfiles] Pushing local changes..."
  git add -A
  git commit -m "update dotfiles"
  git push
else
  echo "[dotfiles] No local changes"
fi

# Pull remote changes
echo "[dotfiles] Pulling remote changes..."
git pull --rebase

echo "[dotfiles] Synced. Reload tmux (Ctrl+s r) or restart nvim if needed."
