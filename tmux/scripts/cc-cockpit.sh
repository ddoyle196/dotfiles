#!/bin/zsh
# Open the cockpit: one session holding the list and the conversation you picked,
# built once and reused, so coming back lands you where you were.
#   --ensure   build it if missing, then stop (tmux does the switching)
emulate -L zsh
S=cockpit
DIR="$HOME/.tmux/scripts"
CWD="${COCKPIT_DIR:-$HOME/Dev/project}"
WIDTH=54

# The legend lives in the status line: two rows of content and no border row,
# where a pane of its own cost three.
keybar() {
  local sess=$1 w n
  w=$(tmux display -p -t "$sess" '#{client_width}' 2>/dev/null)
  [[ -n $w && $w -gt 0 ]] || w=$(tmux display -p -t "$sess" '#{window_width}' 2>/dev/null)
  n=$("$DIR/cc-keys.sh" fit "${w:-186}")
  tmux set -t "$sess" status "$n"
  tmux set -t "$sess" status-position bottom
  tmux set -t "$sess" status-style "bg=default"
  tmux set -t "$sess" status-interval 60
  local i
  for (( i=1; i<=n; i++ )); do
    tmux set -t "$sess" "status-format[$(( i - 1 ))]" \
      "#($DIR/cc-keys.sh format $i #{client_width})"
  done
  # A narrower client needs more rows, so recount whenever the size changes.
  tmux set-hook -t "$sess" client-resized "run-shell '$DIR/cc-cockpit.sh --keybar'"
}

if [[ ${1:-} == --keybar ]]; then keybar "$S"; exit 0; fi

if ! tmux has-session -t "=$S" 2>/dev/null; then
  # Born at the client's size: a session created at the default 80 columns has
  # its panes scaled proportionally on attach, which quietly ruins the width.
  size=(${(s:x:)$(tmux display -p '#{client_width}x#{client_height}' 2>/dev/null)})
  [[ -n ${size[1]:-} ]] || size=(200 50)

  # Pane ids, not indexes: pane-base-index differs between configs and getting
  # it wrong silently resizes the conversation instead of the list.
  list=$(tmux new-session -d -P -F '#{pane_id}' -s "$S" -n cockpit -c "$CWD" \
           -x "$size[1]" -y "$size[2]" "$DIR/cc-run-panel.sh")
  stage=$(tmux split-window -h -P -F '#{pane_id}' -t "$list" -c "$CWD" "$DIR/cc-stage.sh")
  tmux set -t "$S" @list_pane  "$list"
  tmux set -t "$S" @stage_pane "$stage"
  tmux set -t "$S" @list_width "$WIDTH"
  tmux set -t "$S" pane-border-status off
  # Attaching from a different-sized terminal rescales panes; put the list back.
  tmux set-hook -t "$S" client-attached \
    "resize-pane -t '#{@list_pane}' -x '#{@list_width}'"
  keybar "$S"
  tmux resize-pane -t "$list" -x "$WIDTH"
  tmux select-pane -t "$list"
fi

# An existing cockpit may predate the status-line legend, or still carry the old
# pane version of it. Heal rather than making you tear the session down.
old=$(tmux show -t "$S" -v @keys_pane 2>/dev/null)
if [[ -n $old ]]; then
  tmux kill-pane -t "$old" 2>/dev/null
  tmux set -u -t "$S" @keys_pane 2>/dev/null
  tmux set -u -t "$S" @keys_height 2>/dev/null
fi
[[ -z $(tmux show -t "$S" -v @list_pane 2>/dev/null) ]] && {
  list=$(tmux list-panes -t "$S" -F '#{pane_id} #{pane_start_command}' 2>/dev/null \
         | grep -E 'cc-(run-)?panel.sh' | head -1 | cut -d' ' -f1)
  [[ -n $list ]] && {
    tmux set -t "$S" @list_pane "$list"
    tmux set -t "$S" @list_width "$(tmux display -p -t "$list" '#{pane_width}')"
    stage=$(tmux list-panes -t "$S" -F '#{pane_id}' | grep -vx "$list" | head -1)
    [[ -n $stage ]] && tmux set -t "$S" @stage_pane "$stage"
  }
}
keybar "$S"

[[ ${1:-} == --ensure ]] && exit 0
if [[ -n ${TMUX:-} ]]; then tmux switch-client -t "$S"; else tmux attach -t "$S"; fi
