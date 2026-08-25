#!/bin/zsh
# Keeps the list pane alive across a bad edit. The panel re-execs itself when its
# file changes and `zsh -n` only catches syntax, so a runtime error — a malformed
# associative array, say — used to take the pane down with it and leave the
# cockpit with no list at all.
emulate -L zsh
PANEL="$HOME/.tmux/scripts/cc-panel.sh"
LOG="${TMPDIR:-/tmp}/cc-panel-$UID.log"
RS=$'\e[0m'; RED=$'\e[38;2;209;77;65m'; MUT=$'\e[38;2;135;133;128m'

while true; do
  "$PANEL" "$@"
  rc=$?
  set --                      # a restart is a fresh start, not a resumed state
  (( rc == 0 )) && exit 0     # q, and only q, closes the cockpit
  print -n $'\e[?25h\e[?1049l'
  print -r -- ""
  print -r -- "  ${RED}The cockpit list exited (${rc}).${RS} ${MUT}Restarting in 3s.${RS}"
  print -r -- ""
  tail -6 "$LOG" 2>/dev/null | sed 's/^/  /'
  sleep 3
done
