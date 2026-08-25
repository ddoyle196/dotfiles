# Where the cockpit starts new conversations.
#
# Resolved in this order so that no machine needs its shell rc edited by hand:
#   COCKPIT_DIR in the environment  - an explicit override, wins outright
#   ~/.claude/cockpit/dir           - what setup.sh recorded for this machine
#   $HOME                           - a fresh clone still runs, just from home
#
# A file rather than an exported variable, because these scripts are run by the
# tmux server, which inherits the environment of whatever started it long ago
# and not the one your current shell has.
_cc_dir() {
  local d="${COCKPIT_DIR:-}"
  [ -n "$d" ] || d=$(cat "$HOME/.claude/cockpit/dir" 2>/dev/null)
  [ -n "$d" ] || d="$HOME"
  case "$d" in "~"/*) d="$HOME/${d#\~/}" ;; esac
  printf '%s\n' "$d"
}
