#!/usr/bin/env bash
# Put the pane labels back after tmux-resurrect has rebuilt the panes.
# Missing panes are skipped rather than fatal: a layout can come back smaller
# than it was saved, and a failed label is not worth aborting the restore.
set -uo pipefail
in="$HOME/.tmux/resurrect/labels.txt"
[ -f "$in" ] || exit 0
while IFS=$'\t' read -r target label; do
  [ -n "${target:-}" ] && [ -n "${label:-}" ] || continue
  tmux set -p -t "$target" @label "$label" 2>/dev/null || true
done < "$in"
