#!/bin/zsh
# The right-hand pane when no conversation is on it. It never exits, so the
# split holds its shape whatever happens to the conversation it was showing.
emulate -L zsh
RS=$'\e[0m'; TX=$'\e[38;2;206;205;195m'; MUT=$'\e[38;2;135;133;128m'; YEL=$'\e[38;2;208;162;21m'

# --parked <label> is what the right pane shows while the cursor rests on a
# conversation that is not running: following the cursor must never start one.
PARKED=""; EMPTY=""
[[ ${1:-} == --parked ]] && PARKED=${2:-}
[[ ${1:-} == --empty ]]  && EMPTY=${2:-}

paint() {
  print -n $'\e[2J\e[H'
  if [[ -n $EMPTY ]]; then
    print -r -- ""
    print -r -- "   ${TX}${EMPTY}${RS}"
    print -r -- ""
    print -r -- "   ${MUT}No conversations in this topic yet.${RS}"
    print -r -- ""
    print -r -- "   ${YEL}n${RS} ${MUT}starts the first one${RS}"
    return
  fi
  if [[ -n $PARKED ]]; then
    print -r -- ""
    print -r -- "   ${TX}${PARKED}${RS}"
    print -r -- ""
    print -r -- "   ${MUT}Parked — nothing is running.${RS}"
    print -r -- ""
    print -r -- "   ${YEL}enter${RS} ${MUT}picks it back up where it left off${RS}"
    return
  fi
  print -r -- ""
  print -r -- "   ${TX}Pick a conversation on the left.${RS}"
  print -r -- ""
  print -r -- "   ${YEL}enter${RS} ${MUT}open it${RS}          ${YEL}t${RS} ${MUT}new topic${RS}"
  print -r -- "   ${YEL}f${RS}     ${MUT}follow cursor${RS}    ${YEL}n${RS} ${MUT}new conversation${RS}"
  print -r -- "   ${YEL}o${RS}     ${MUT}reorder${RS}          ${YEL}i${RS} ${MUT}bring back a past one${RS}"
  print -r -- "   ${YEL}z${RS}     ${MUT}fold finished${RS}    ${YEL}?${RS} ${MUT}all keys${RS}"
  print -r -- ""
  print -r -- "   ${MUT}ctrl-h comes back to the list from any conversation.${RS}"
}

# The outer pane repaints itself once the client that was here finishes leaving,
# so the first paint can be wiped; paint again after it settles.
TRAPWINCH() { paint }
paint
sleep 0.5
paint
while true; do read -s -k1 2>/dev/null && paint || sleep 3600; done
