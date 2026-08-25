#!/usr/bin/env bash
# Persist the per-pane @label option across a restart. tmux-resurrect saves
# panes but not custom pane options, so the labels set with `prefix + T` would
# come back blank without this. Keyed by session:window.pane, which is the same
# identity resurrect restores a pane into.
set -uo pipefail
out="$HOME/.tmux/resurrect/labels.txt"
mkdir -p "$(dirname "$out")"
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}	#{@label}' 2>/dev/null \
  | awk -F'\t' 'NF == 2 && $2 != ""' > "$out.tmp$$" && mv "$out.tmp$$" "$out"
