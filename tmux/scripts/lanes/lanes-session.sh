#!/bin/zsh
# Open the lanes view: one session, the lanes list on the left and the chat you
# talk to on the right. Built once and reused.
#   --ensure   build it if missing, then stop (no attach/switch)
#   --help     this text
# Test seams: LANES_TMUX_SOCKET (private tmux server), LANES_CLAUDE (chat command),
# LANES_TUI (list program), LANES_DB, LANES_STATE_DIR.
emulate -L zsh
S=lanes
HERE=${0:A:h}
source "$HOME/.tmux/scripts/lib/cc-dir.sh"
CWD=$(_cc_dir)
STATE=${LANES_STATE_DIR:-$HOME/.claude/helm}
DB=${LANES_DB:-$STATE/helm.db}
TUI=${LANES_TUI:-$HERE/lanes-tui.py}
CHAT_WIDTH_PCT=55

# A private test server skips the user's tmux.conf: its plugins and hooks act on
# the real session store.
t() { command tmux ${LANES_TMUX_SOCKET:+-f} ${LANES_TMUX_SOCKET:+/dev/null} ${LANES_TMUX_SOCKET:+-L} ${LANES_TMUX_SOCKET:+$LANES_TMUX_SOCKET} "$@" }

case "${1:-}" in
  -h|--help)
    sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
esac

# The helm conversations run unattended, so it skips permission prompts; the
# main chat is the same kind of session and gets the same flags as cc-host.sh.
chat_command() {
  local sid=$1 effort=${HELM_EFFORT:-medium} sub=${HELM_SUBAGENT_MODEL:-sonnet}
  local -a cmd=( env CC_LANES_MAIN=1 )
  [[ -n $sub && $sub != none ]] && cmd+=( CLAUDE_CODE_SUBAGENT_MODEL=$sub )
  cmd+=( ${=LANES_CLAUDE:-claude} --dangerously-skip-permissions )
  [[ -n $effort && $effort != none ]] && cmd+=( --effort "$effort" )
  (( ${HELM_ULTRACODE:-1} )) && cmd+=( --settings '{"ultracode":true}' )
  [[ -n $sid ]] && cmd+=( --resume "$sid" )
  print -r -- "${(q)cmd[@]}"
}

# Only resume a conversation whose transcript still exists, as cc-host.sh does:
# --resume on a missing one drops the pane into an error instead of a chat.
main_session() {
  local sid proj
  [[ -f $DB ]] || return 0
  sid=$(python3 -c '
import sqlite3, sys
try:
    row = sqlite3.connect(sys.argv[1], timeout=1).execute(
        "SELECT v FROM meta WHERE k = ?", ("main_session_id",)).fetchone()
except sqlite3.Error:
    row = None
print(row[0] if row else "")' "$DB" 2>/dev/null)
  proj="${CLAUDE_PROJECTS:-$HOME/.claude/projects}/${CWD//[\/._]/-}"
  [[ -n $sid && -f $proj/$sid.jsonl ]] && print -r -- "$sid"
  return 0
}

# A session made at tmux's default 80x24 and resized later squeezes the split;
# sizing it to the attaching client up front keeps the 38/62 split true.
client_size() {
  local w h
  w=$(t display -p '#{client_width}' 2>/dev/null)
  h=$(t display -p '#{client_height}' 2>/dev/null)
  [[ $w == <-> && $h == <-> ]] || read -r h w < <(stty size 2>/dev/null)
  [[ $w == <-> && $h == <-> ]] || { w=200; h=50 }
  print "$w $h"
}

# The chat pane's border carries a chip naming the lane it is on, in that
# lane's topic colour (lanes-tui.py sets @lane_label/@lane_color), ahead of the
# usual pane label. Window-scoped, so other sessions keep their own borders.
LANE_CHIP='#{?@lane_label,#[bold]#[fg=#100f0f bg=#{@lane_color}] #{@lane_label} #[default] ,}'
style_borders() {
  local fmt
  fmt=$(t show-options -wqv -t "=$S:" pane-border-format)
  [[ $fmt == *@lane_label* ]] && return 0
  fmt=$(t show-options -gwqv pane-border-format)
  t set-option -w -t "=$S:" pane-border-format "${LANE_CHIP}${fmt}"
  [[ $(t show-options -gwqv pane-border-status) == off ]] &&
    t set-option -w -t "=$S:" pane-border-status bottom
  return 0
}

add_list() {
  local chat=$1 tui
  tui=$(t split-window -h -b -t "$chat" -l "$(( 100 - CHAT_WIDTH_PCT ))%" -c "$HERE" -P -F '#{pane_id}' \
    "python3 ${(q)TUI}") || return 1
  t set-option -t "=$S:" @lanes_tui_pane "$tui"
  t select-pane -t "$tui"
}

build() {
  local w h chat
  read -r w h <<< "$(client_size)"
  chat=$(t new-session -d -s "$S" -x "$w" -y "$h" -c "$CWD" -P -F '#{pane_id}' \
    "$(chat_command "$(main_session)")") || return 1
  t set-option -t "=$S:" @lanes_chat_pane "$chat"
  style_borders
  add_list "$chat"
}

# The list can exit (quit, crash) while the chat lives on; reopening the view
# should bring it back rather than leave a chat-only session.
repair() {
  local tui chat
  tui=$(t show-option -qv -t "=$S:" @lanes_tui_pane)
  [[ -n $tui ]] && t list-panes -s -t "=$S" -F '#{pane_id}' | grep -qx -- "$tui" && return 0
  chat=$(t show-option -qv -t "=$S:" @lanes_chat_pane)
  t list-panes -s -t "=$S" -F '#{pane_id}' | grep -qx -- "$chat" ||
    chat=$(t list-panes -t "=$S:" -F '#{pane_id}' | head -1)
  t set-option -t "=$S:" @lanes_chat_pane "$chat"
  add_list "$chat"
}

if t has-session -t "=$S" 2>/dev/null; then repair; style_borders; else build || exit 1; fi
[[ ${1:-} == --ensure ]] && exit 0

if [[ -n ${TMUX:-} ]]; then
  t switch-client -t "=$S"
else
  t attach-session -t "=$S"
fi
