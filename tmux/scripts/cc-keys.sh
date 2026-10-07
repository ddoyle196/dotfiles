#!/bin/zsh
# The cockpit key legend, rendered into the session's status line rather than a
# pane of its own: a pane costs a border row on top of its content, and that row
# is a row of nothing.
#
#   cc-keys.sh format <line> <width>   one status line, in tmux style tags
#   cc-keys.sh fit <width>             how many lines the content needs
emulate -L zsh

# tmux style tags, not ANSI: the status line speaks its own dialect.
T_KEY="#[fg=#d0a215]"; T_MUT="#[fg=#878580]"; T_FAINT="#[fg=#575653]"
T_RED="#[fg=#d14d41]"; T_YEL="#[fg=#d0a215]"; T_BLU="#[fg=#4385be]"
T_GRN="#[fg=#879a39]"; T_DEF="#[fg=default]"

# key, what it does. Ordered by how often you reach for it.
KEYS=(
  "enter:open"      "a:reply"         "tab:wake"        "/:jump"
  "q ^s a:exit"
  "j k:up/down"     "{}:section"      "g G:first/last"
  "f:follow"        "o:order"         "z:fold"          "v:view"
  "< >:width"       "n:new"           "t:topic"         "i:import"
  "r:rename"        "T:move"          "x:park"          "w:wait"
  "e:file"          "d:remove"        "D:drop topic"
  "u:usage"         "R:refresh"       "^l:redraw"       "^r:reload"
  "^h:list"
)
# glyph, colour, label — kept apart so the width can be counted without having
# to strip style tags back out of a rendered string. Braces are load-bearing:
# `$T_RED:answer` would apply zsh's `:a` modifier and eat the label.
STATES=(
  "⬤:${T_RED}:answer"  "◐:${T_YEL}:running"  "○:${T_BLU}:pick up"
  "◌:${T_MUT}:waiting" "✓:${T_GRN}:done"     "▪:${T_FAINT}:parked"
  "•:#[fg=#83b3e3]:unread" "⧗:${T_YEL}:blocked"
)

# Pull request badges, the same glyphs and colours the rows draw them with.
# `unknown` is deliberately absent: it renders as the same dot as a parked
# conversation and explaining it would cost more than it is worth.
# Only the marks that change what you would do. Draft, closed and merged read
# as themselves once you have seen the set, and the bar is worth more to the
# keys than to a glossary.
PRS=(
  "✗:${T_RED}:fail"       "◷:${T_YEL}:pending"  "✓:${T_GRN}:approved"
  "○:${T_BLU}:open"
)

W=80

# Flows items onto as many lines as fit. Widths are counted on the plain text,
# so the style tags never push a line over.
flow() {   # flow <widths array name> <rendered array name> -> LINES_OUT
  local -a w=("${(@P)1}") r=("${(@P)2}")
  local i line="" plain=0
  LINES_OUT=()
  for (( i=1; i<=${#r}; i++ )); do
    # A zero-width item is a forced break: the marks start a row of their own
    # rather than trailing the keys and leaving two glyphs orphaned below.
    if (( w[i] == 0 )); then
      [[ -n $line ]] && { LINES_OUT+=("$line"); line=""; plain=0 }
      continue
    fi
    if (( plain && plain + 3 + w[i] > W - 2 )); then
      LINES_OUT+=("$line"); line=""; plain=0
    fi
    (( plain )) && { line+="   "; (( plain += 3 )) }
    line+="${r[i]}"; (( plain += w[i] ))
  done
  [[ -n $line ]] && LINES_OUT+=("$line")
}

build_lines() {   # -> LINES_OUT
  local -a iw ir
  local item k d g c
  for item in "${KEYS[@]}"; do
    k=${item%%:*}; d=${item#*:}
    iw+=( $(( ${#k} + 1 + ${#d} )) )
    ir+=( "${T_KEY}${k}${T_MUT} ${d}" )
  done
  # The marks open a row of their own, so no divider stands between the two.
  iw+=( 0 ); ir+=( "" )
  local sep="" sepw=0
  for item in "${STATES[@]}"; do
    g=${item%%:*}; c=${${item#*:}%:*}; d=${item##*:}
    iw+=( $(( sepw + 2 + ${#d} )) )
    ir+=( "${sep}${c}${g}${T_MUT} ${d}" )
    sep=""; sepw=0
  done
  # A divider rides with the first pull-request glyph rather than standing
  # alone, so the badges read as a set and a wrap never leaves it dangling.
  sep="${T_FAINT}│   "; sepw=4
  for item in "${PRS[@]}"; do
    g=${item%%:*}; c=${${item#*:}%:*}; d=${item##*:}
    iw+=( $(( sepw + 2 + ${#d} )) )
    ir+=( "${sep}${c}${g}${T_MUT} ${d}" )
    sep=""; sepw=0
  done
  flow iw ir
}

case "${1:-}" in
  format)
    W=${3:-80}; (( W < 20 )) && W=20
    build_lines
    print -rn -- " ${LINES_OUT[${2:-1}]:-}${T_DEF}"
    ;;
  fit)
    W=${2:-80}; (( W < 20 )) && W=20
    build_lines
    print -r -- ${#LINES_OUT}
    ;;
  *)
    print -u2 "usage: cc-keys.sh format <line> <width> | fit <width>"
    exit 2 ;;
esac
