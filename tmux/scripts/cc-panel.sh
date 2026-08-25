#!/bin/zsh
# The cockpit, drawn in a tmux pane: the list on the left, the conversation you
# picked on the right. Same registry and states as the nvim cockpit — only the
# drawing differs, so the two stay in step by construction.
emulate -L zsh
setopt no_nomatch
zmodload zsh/datetime    # EPOCHSECONDS
zmodload zsh/system      # sysparams[pid], which unlike $$ differs in a subshell

HOST="$HOME/.tmux/scripts/cc-host.sh"
STAGE="$HOME/.tmux/scripts/cc-stage.sh"
CACHE="${TMPDIR:-/tmp}/cc-panel-$UID.json"
LOG="${TMPDIR:-/tmp}/cc-panel-$UID.log"

# Anything written to the pane outside draw() scrolls it, which throws off both
# the diff renderer and the eye — that is what the flicker was. Errors go to a
# log instead, where they can still be read.
exec 2>>"$LOG"
SEP=$'\x01'
SPACES="${(l:400:: :)}"

# Flexoki, the same palette the pane borders and labels already use.
RS=$'\e[0m'
C_RED=$'\e[38;2;209;77;65m'
C_YEL=$'\e[38;2;208;162;21m'
C_BLU=$'\e[38;2;67;133;190m'
C_GRN=$'\e[38;2;135;154;57m'
C_TX=$'\e[38;2;206;205;195m'
C_MUT=$'\e[38;2;135;133;128m'
C_FAINT=$'\e[38;2;87;86;83m'
BG_SEL=$'\e[48;2;64;62;60m'
# Unread rows carry their own ground, so a conversation that has spoken since
# you looked reads as a block rather than a dot you have to hunt for.
BG_NEW=$'\e[48;2;16;36;58m'
# The cursor gets a bar down the whole row block: a tint alone is not something
# the eye finds at a glance in a list this long.
C_BAR=$'\e[38;2;218;215;205m'
C_TOPIC=$'\e[38;2;58;169;159m'
C_MAG=$'\e[38;2;138;97;213m'

typeset -A ICON FG SNAME
# One family, one optical weight: mixing a solid diamond with a hairline
# triangle made the column read as noise rather than a scale.
ICON=( answer "●" running "◐" pickup "○" waiting "◌" done "✓" dead "·" empty "" )
FG=(   answer $C_RED running $C_YEL pickup $C_BLU waiting $C_MUT done $C_GRN dead $C_FAINT empty $C_FAINT )
SNAME=( answer "answer this" running "running" pickup "pick this up" \
        waiting "waiting on someone" done "finished" dead "process gone" \
        empty "empty" )

# One glyph per pull request, in the same family as the conversation states.
typeset -A PR_ICON PR_FG
PR_ICON=( fail "✗" pending "◷" draft "⊘" approved "✓" open "○" merged "⤳" closed "×" unknown "·" )
PR_FG=(   fail $C_RED pending $C_YEL draft $C_FAINT approved $C_GRN \
          open $C_BLU merged $C_MAG closed $C_FAINT unknown $C_FAINT )

typeset -A ORDER_NAME
ORDER_NAME=( act "actionability" topic "topic" age "recency" )
ORDERS=( act topic age )

ORDER=act
FOLDED=1
FILTER=""       # live fuzzy filter, empty when not filtering
REAP_HOURS=8    # stop the process behind a conversation nobody has touched
REAPED_AT=0
FOLLOW=1        # the right pane tracks the cursor on its own
SHOWN=""        # what the right pane is currently showing, as "<id>:<cold>"
SHOWN_AT=0      # when it went up, so a scroll-past does not count as reading
SEEN_MARKED=""  # the id already marked read for this showing
PEND_AT=0       # when the cursor last moved, so a scroll does not thrash it
CUR=1
TOP=1
MSG=""

# Editing this file should be enough to see the change: the idle tick notices a
# new mtime and re-execs, carrying the cursor and the view across.
SELF="${0:A}"
SELF_MTIME=$(stat -f %m "$SELF" 2>/dev/null)
if [[ ${1:-} == --state ]]; then
  IFS=: read -r CUR TOP ORDER FOLDED FOLLOW <<< "$2"
  FOLLOW=${FOLLOW:-1}
fi

reload() {
  kill $REFRESHER 2>/dev/null
  stty echo 2>/dev/null
  # Re-fit the status-line legend too, so one reload refreshes the whole cockpit.
  tmux run-shell -b "$HOME/.tmux/scripts/cc-cockpit.sh --keybar" 2>/dev/null
  print -n $'\e[?25h\e[?1049l'
  exec "$SELF" --state "${CUR}:${TOP}:${ORDER}:${FOLDED}:${FOLLOW}"
}

# $COLUMNS and $LINES are not maintained in a non-interactive script parked in
# read(): they keep their startup values through every resize. Ask the tty and
# set them, so everything downstream can go on using them.
winsize() {
  local sz=(${=$(stty size 2>/dev/null)})
  (( ${#sz} == 2 )) || return
  LINES=$sz[1]; COLUMNS=$sz[2]
}

# data ----------------------------------------------------------------------

# The background refresher shares $$ with the panel, so a fixed staging name
# let the two races clobber each other and mv complained into the pane.
refresh_now() {
  local staging="${CACHE}.${sysparams[pid]}"
  "$HOST" list > "$staging" 2>/dev/null && mv -f "$staging" "$CACHE" 2>/dev/null
  rm -f "$staging" 2>/dev/null
}

load() {
  r_id=(); r_topic=(); r_label=(); r_recap=(); r_state=(); r_cold=(); r_upd=()
  r_new=(); r_prs=()
  local id topic label recap state cold upd new prs
  while IFS=$SEP read -r id topic label recap state cold upd new prs; do
    r_id+=("$id"); r_topic+=("$topic"); r_label+=("$label"); r_recap+=("$recap")
    r_state+=("$state"); r_cold+=("$cold"); r_upd+=("$upd"); r_new+=("$new")
    r_prs+=("$prs")
  done < <(jq -r --arg s "$SEP" --arg o "$ORDER" '
      def rank: {"answer":1,"running":2,"pickup":3,"waiting":4,"done":5,"dead":6,"empty":7}[.state] // 3;
      def fin:  (if (.state=="done" or .state=="dead") and .cold then 1 else 0 end);
      [ .[] ]
      | (if   $o=="topic" then sort_by([fin, (.topic == ""), .topic, rank, -(.updated_at)])
         elif $o=="age"   then sort_by([fin, -(.updated_at)])
         else                  sort_by([fin, rank, -(.updated_at)]) end)
      | .[] | [.id, .topic, .label, .recap, .state,
               (if .cold then "1" else "0" end), (.updated_at|tostring),
               (if .unread then "1" else "0" end),
               ([(.prs // [])[] | "\(.mark) \(.label)"] | join(","))] | join($s)
    ' "$CACHE" 2>/dev/null)
  NROWS=${#r_id}
  resolve_sel
  # New data can change a whole block of lines at once — a row going unread
  # repaints its recap too — and a partially-applied frame left rows half
  # tinted. Keystrokes do not come through here, so the diff still does its job
  # where it matters.
  PREV=()
  DIRTY=1
}

# The list re-sorts whenever a state changes, so a row number is not a
# selection: opening a conversation moved it and left the cursor on a stranger.
resolve_sel() {
  local i=0
  if [[ -n ${SEL_ID:-} ]]; then
    for (( i=1; i<=NROWS; i++ )); do
      [[ $r_id[i] == $SEL_ID ]] && { CUR=$i; return }
    done
  fi
  (( CUR > NROWS )) && CUR=$NROWS
  (( CUR < 1 ))     && CUR=1
  SEL_ID=${r_id[$CUR]:-}
}

# Subsequence matching over "label topic", turned into one glob so a keystroke
# costs no forks: "rmtco" finds "RMT control via Glean MCP".
matches() {   # matches <row>
  [[ -z $FILTER ]] && return 0
  local q=${FILTER:l} c n pat="*"
  for (( n=1; n<=${#q}; n++ )); do
    c=${q[n]}
    [[ $c == ' ' ]] && continue
    case $c in [\*\?\[\]\(\)\|\<\>\^\#\~\\]) c="\\$c" ;; esac
    pat+="${c}*"
  done
  [[ "${r_label[$1]:l} ${r_topic[$1]:l}" == ${~pat} ]]
}

# A subsequence match alone lands on the wrong row: "rmt" finds the r, m and t
# scattered through an unrelated label before it finds the RMT one. The list
# order stays put — only the cursor prefers the better match.
match_rank() {   # match_rank <row> -> MRANK, lower is better
  local q=${FILTER:l} lab=${r_label[$1]:l} top=${r_topic[$1]:l}
  if   [[ $lab == ${q}* ]];  then MRANK=1
  elif [[ $top == ${q}* ]];  then MRANK=2
  elif [[ $lab == *${q}* ]]; then MRANK=3
  elif [[ $top == *${q}* ]]; then MRANK=4
  else                            MRANK=5; fi
}

# Keeps the cursor on something the filter still shows.
snap_match() {   # snap_match [dir]
  local dir=${1:-0} i best=9 besti=0
  if (( dir == 0 )); then
    for (( i=1; i<=NROWS; i++ )); do
      matches $i || continue
      match_rank $i
      (( MRANK < best )) && { best=$MRANK; besti=$i }
    done
    (( besti )) && { CUR=$besti; SEL_ID=$r_id[$besti] }
    return
  fi
  for (( i=CUR+dir; i>=1 && i<=NROWS; i+=dir )); do
    matches $i && { CUR=$i; SEL_ID=$r_id[$i]; return }
  done
}

# A topic is only as calm as its most demanding conversation.
typeset -A RANK
RANK=( answer 1 running 2 pickup 3 waiting 4 done 5 dead 6 empty 7 )

topic_icon() {   # -> TICON
  local t=$1 best=9 i rk besti=0
  for (( i=1; i<=NROWS; i++ )); do
    [[ $r_topic[i] != $t ]] && continue
    rk=${RANK[$r_state[i]]:-3}
    (( rk < best )) && { best=$rk; besti=$i }
  done
  TICON=${ICON[$r_state[$besti]]-○}
}

# Whatever the current order groups by, so the list reads as sections rather
# than one long run of rows.
section_of() {   # section_of <row> -> SECT
  local i=$1 m
  case $ORDER in
    topic)
      topic_icon $r_topic[i]
      SECT=${TICON:+"${TICON} "}"${r_topic[i]:-unfiled}" ;;
    age)
      if (( r_upd[i] == 0 )); then SECT="just made"
      else
        m=$(( (EPOCHSECONDS - r_upd[i]) / 60 ))
        if   (( m < 1440 ));  then SECT="today"
        elif (( m < 10080 )); then SECT="this week"
        else                       SECT="older"; fi
      fi ;;
    *) SECT=${SNAME[$r_state[i]]:-other} ;;
  esac
}

age_of() {   # -> AGE
  local u=$1 m
  (( u == 0 )) && { AGE=""; return }
  m=$(( (EPOCHSECONDS - u) / 60 ))
  if   (( m < 60 ));   then AGE="${m}m"
  elif (( m < 1440 )); then AGE="$(( m / 60 ))h"
  else                      AGE="$(( m / 1440 ))d"; fi
}

# Two lines is the whole budget; a recap cut short says so.
wrap2() {
  WRAPPED=()
  local text=$1 width=$2 line="" word clipped=0
  for word in ${=text}; do
    if (( ${#line} + ${#word} + 1 > width )); then
      (( ${#WRAPPED} >= 2 )) && { clipped=1; break }
      WRAPPED+=("$line"); line=$word
    else
      [[ -z $line ]] && line=$word || line="$line $word"
    fi
  done
  (( ${#WRAPPED} < 2 )) && [[ -n $line ]] && WRAPPED+=("$line")
  (( clipped )) && (( ${#WRAPPED} > 0 )) && WRAPPED[-1]="${WRAPPED[-1]}…"
}

# layout --------------------------------------------------------------------

add() {
  L_kind+=("$1"); L_a+=("$2"); L_b+=("$3"); L_fg+=("$4"); L_row+=("$5")
  L_c+=("${6:-}"); L_vis+=("${7:-${#2}}")
}

build() {
  L_kind=(); L_a=(); L_b=(); L_fg=(); L_row=(); L_c=(); L_vis=(); ROWLINE=()
  local l badges badge_w b mk lb inner=$(( COLUMNS - 9 )) lw=$(( COLUMNS - 12 ))
  (( inner < 16 )) && inner=16
  (( lw < 12 )) && lw=12

  local folded_txt="finished shown" fincount=0 convos=0 i
  (( FOLDED )) && folded_txt="finished folded"
  for (( i=1; i<=NROWS; i++ )); do
    # Only a finished conversation that is also parked is out of the way. One
    # with a live process is still something you can talk to, and folding it
    # made whole topics vanish from the list.
    [[ ( $r_state[i] == done || $r_state[i] == dead ) && $r_cold[i] == 1 ]] && (( fincount++ ))
    [[ -n $r_id[i] ]] && (( convos++ ))    # placeholders for empty topics do not count
  done

  if [[ -n $FILTER ]]; then
    add text "  filtering" "" $C_TOPIC 0
  else
    add text "  ${convos} conversations   ${ORDER_NAME[$ORDER]}   ${folded_txt}" "" $C_FAINT 0
  fi
  add blank "" "" "" 0

  local seen="" st lab dim
  for (( i=1; i<=NROWS; i++ )); do
    st=$r_state[i]
    [[ $st == empty && $ORDER != topic && $i -ne $CUR ]] && continue
    matches $i || continue
    # A search looks everywhere, so filtering overrides folding.
    [[ -z $FILTER && $FOLDED == 1 && ( $st == done || $st == dead ) \
       && $r_cold[i] == 1 && $r_id[i] != ${SEL_ID:-} ]] && continue
    section_of $i
    if [[ $SECT != $seen ]]; then
      (( ${#L_kind} > 2 )) && add blank "" "" "" 0
      add sect "$SECT" "" "$C_FAINT" 0
      seen=$SECT
    fi
    dim=${FG[$st]:-$C_BLU}
    [[ $st == done || $st == dead ]] && dim=$C_FAINT
    local mark=" " markc=""
    if [[ $r_new[i] == 1 && $st != empty ]]; then
      mark="•"; markc=$'\e[38;2;131;179;227m' 
    elif [[ $r_cold[i] == 1 && $st != dead ]]; then
      mark="·"; markc=$C_FAINT
    fi
    local recap_fg=$C_MUT
    [[ $st == done || $st == dead ]] && recap_fg=$C_FAINT
    ROWLINE[$i]=$(( ${#L_kind} + 1 ))
    age_of $r_upd[i]
    # Under any order but topic, the row is the only place the topic can show.
    local tag=""
    [[ $ORDER != topic ]] && tag=$r_topic[i]
    lw=$(( COLUMNS - 12 - ${#tag} ))
    (( lw < 12 )) && lw=12
    lab=$r_label[i]
    (( ${#lab} > lw )) && lab="${lab[1,lw]}…"
    if [[ $st == empty ]]; then
      add head "     ${C_FAINT}nothing here yet — n starts a conversation" "" "$C_FAINT" "$i" "" 47
      continue
    fi
    add head "  ${markc}${mark}${dim}${ICON[$st]:-○} ${lab}" "$tag" "$dim" "$i" "$AGE" \
        $(( 5 + ${#lab} ))
    wrap2 "$r_recap[i]" $inner
    for l in $WRAPPED; do add recap "       $l" "" "$recap_fg" "$i"; done
    if [[ -n ${r_prs[i]:-} ]]; then
      badges=""; badge_w=0
      for b in ${(s:,:)r_prs[i]}; do
        mk=${b%% *}; lb=${b#* }
        badges+="${PR_FG[$mk]:-$C_FAINT}${PR_ICON[$mk]:-·} ${C_MUT}${lb}  "
        (( badge_w += ${#lb} + 4 ))
      done
      add recap "       ${badges}" "" "$C_MUT" "$i" "" $(( 7 + badge_w ))
    fi
  done

  if [[ -n $FILTER ]] && (( ${#ROWLINE} == 0 )); then
    add text "  nothing matches \"${FILTER}\"" "" "$C_FAINT" 0
  fi
  if [[ -z $FILTER ]] && (( fincount > 0 && FOLDED )); then
    add blank "" "" "" 0
    add text "  › ${fincount} finished  (z)" "" "$C_FAINT" 0
  fi
  if (( NROWS == 0 )); then
    add text "  nothing yet — n starts a conversation" "" "$C_FAINT" 0
    add text "  i brings back a past conversation" "" "$C_FAINT" 0
  fi
}

# Sets LINE. A $(compose) per line cost 49 forks and ~26ms a keystroke, which is
# what made the list look like it was bouncing.
compose() {
  local i=$1 bg="" bar="" kind=${L_kind[$i]:-blank} row=${L_row[$i]:-0}
  if (( row > 0 && row == CUR )); then
    bg=$BG_SEL
    bar="${C_BAR}▌"
  elif (( row > 0 )) && [[ ${r_new[$row]:-0} == 1 ]]; then
    bg=$BG_NEW
  fi
  # Padding, not \e[K, whenever the line carries a background.
  # The bar occupies the leading space every row line already spends, so the
  # columns stay put whether or not the row is the selected one.
  local vis=${L_vis[$i]:-0} pad body=${L_a[$i]}
  [[ -n $bar ]] && body=${body#?}
  case $kind in
    blank)
      if [[ -n $bg ]]; then LINE="${bg}${SPACES[1,$(( COLUMNS - 1 ))]}"
      else LINE=$'\e[K'; fi ;;
    sect)
      local lbl=${L_a[$i]} rule
      rule=$(( COLUMNS - ${#lbl} - 5 ))
      LINE="${bg}${C_FAINT}  ${lbl} "
      (( rule > 0 )) && LINE+="${(l:$rule::─:)}"
      LINE+=$'\e[K' ;;
    head)
      local tag=${L_b[$i]} ag=${L_c[$i]} right=0
      [[ -n $tag ]] && (( right += ${#tag} + 1 ))
      [[ -n $ag ]]  && (( right += ${#ag} ))
      LINE="${bg}${bar}${L_fg[$i]}${body}"
      if [[ -n $bg ]]; then
        pad=$(( COLUMNS - vis - right - 1 ))
        (( pad > 0 )) && LINE+="${SPACES[1,$pad]}"
      else
        LINE+=$'\e[K'
      fi
      if (( right )); then
        LINE+=$'\e['"$(( COLUMNS - right ))"'G'
        [[ -n $tag ]] && LINE+="${C_TOPIC}${tag} "
        [[ -n $ag ]]  && LINE+="${C_FAINT}${ag}"
      fi
      ;;
    *)
      LINE="${bg}${bar}${L_fg[$i]}${body}"
      if [[ -n $bg ]]; then
        pad=$(( COLUMNS - vis - 1 ))
        (( pad > 0 )) && LINE+="${SPACES[1,$pad]}"
      else
        LINE+=$'\e[K'
      fi ;;
  esac
  LINE+="${RS}"
}

# Only the screen lines that actually changed get rewritten, and an identical
# frame is not written at all. Repainting every line each tick is what the eye
# reads as flicker, and no amount of synchronised-update wrapping hides it when
# tmux is free to redraw the pane between our writes.
typeset -a PREV
PREV=()

draw() {
  local h=$LINES i n rows
  rows=$(( h - 1 ))
  (( rows < 3 )) && rows=3
  local hl=${ROWLINE[$CUR]:-1}
  (( hl < TOP )) && TOP=$hl
  (( hl + 2 > TOP + rows - 1 )) && TOP=$(( hl + 3 - rows ))
  (( TOP > ${#L_kind} - rows + 1 )) && TOP=$(( ${#L_kind} - rows + 1 ))
  (( TOP < 1 )) && TOP=1

  local -a new
  n=0
  for (( i=TOP; i < TOP + rows; i++ )); do
    compose $i
    new[$(( ++n ))]="$LINE"
  done
  new[$(( ++n ))]=$'\e[K'"${C_FAINT}${MSG}${RS}"

  FRAME=""
  for (( n=1; n<=h; n++ )); do
    [[ ${new[$n]:-} == ${PREV[$n]-$'\0'} ]] && continue
    FRAME+=$'\e['"$n"';1H'"${new[$n]:-$'\e[K'}"
  done
  [[ -z $FRAME ]] && return
  # Synchronised update: the terminal shows the whole frame or none of it.
  print -n -- $'\e[?2026h'"${FRAME}"$'\e[?2026l'
  PREV=("${new[@]}")
}

# input ---------------------------------------------------------------------

note() { MSG="  $1"; draw }

# Prompts paint the bottom two lines behind draw()'s back, so those two rows no
# longer match what it thinks is on screen.
dirty_footer() { PREV[$LINES]=$'\0'; PREV[$(( LINES - 1 ))]=$'\0' }

# Reads a line itself rather than with `read -r`, because that offered no way
# out: enter on an empty line was the only exit, and a two-step prompt like `t`
# went on to create something anyway.
# Completion candidates for the next ask, if it should have any. A global
# because ask's positional arguments already mean something and a third one
# reading as "current" would be easy to get wrong at the call site.
typeset -a ASK_CAND
ASK_CAND=()

_ask_matches() {   # _ask_matches <prefix> -> MATCHES
  MATCHES=()
  local c p=${1:l}
  for c in $ASK_CAND; do
    [[ -n $c && ${c:l} == ${p}* ]] && MATCHES+=("$c")
  done
}

_ask_lcp() {   # longest shared prefix of MATCHES -> LCP
  LCP=${MATCHES[1]:-}
  local m i
  for m in $MATCHES; do
    i=0
    while (( i < ${#LCP} && i < ${#m} )) && [[ ${LCP[i+1]:l} == ${m[i+1]:l} ]]; do
      (( i++ ))
    done
    LCP=${LCP[1,i]}
  done
}

# Every topic in the current view, deduped. Read off the rows rather than asked
# of the host: it is already in memory, and a keystroke should not wait on a
# subprocess to tell it what it just drew.
set_topic_cand() {
  local -A seen; local i t
  ASK_CAND=()
  for (( i=1; i<=NROWS; i++ )); do
    t=$r_topic[i]
    [[ -z $t || -n ${seen[$t]:-} ]] && continue
    seen[$t]=1; ASK_CAND+=("$t")
  done
}

ask() {  # ask <label> [current] -> REPLY; nonzero means backed out
  local label=$1 cur=${2:-} ans="" k seq line
  # stem is what you actually typed; tab cycles the matches for that, not for
  # whatever the last completion left in the buffer.
  local stem="" ghost hint; local -i tabi=0
  local -a MATCHES; local LCP
  local -a CAND; CAND=("${ASK_CAND[@]}"); ASK_CAND=()
  (( ${#label} > COLUMNS - 14 )) && label="${label[1,COLUMNS-15]}…"
  while :; do
    line="${C_TX}  ${label}"
    [[ -n $cur ]] && line+="${C_FAINT} (now: ${cur})${C_TX}"
    # The rest of the best match, shown faint ahead of the cursor, so tab is a
    # visible offer rather than something you have to know is there.
    ghost=""
    if (( ${#CAND} )) && [[ -n $ans ]]; then
      ASK_CAND=("${CAND[@]}"); _ask_matches "$ans"; ASK_CAND=()
      (( ${#MATCHES} )) && [[ ${#MATCHES[1]} -gt ${#ans} ]] && ghost=${MATCHES[1]:${#ans}}
    fi
    hint="  esc backs out"
    (( ${#CAND} )) && hint+="${C_FAINT}  ·  tab completes"
    print -n $'\e['"$(( LINES - 1 ))"';1H'$'\e[K'"${C_FAINT}${hint}${RS}"
    print -n $'\e['"$LINES"';1H'$'\e[K'"${line}: ${ans}${C_FAINT}${ghost}${RS}"
    [[ -n $ghost ]] && print -n $'\e['"${#ghost}"'D'
    print -n $'\e[?25h'
    read -s -k1 k || { ans=""; break }
    case $k in
      $'\t')
        (( ${#CAND} )) || continue
        ASK_CAND=("${CAND[@]}"); _ask_matches "$stem"; ASK_CAND=()
        (( ${#MATCHES} )) || continue
        if (( ${#MATCHES} == 1 )); then
          ans=${MATCHES[1]}
        else
          _ask_lcp
          # Grow to the shared prefix first; only once that stops making
          # progress does tab start walking the candidates one by one.
          if (( ${#LCP} > ${#ans} )); then
            ans=$LCP
          else
            (( tabi = tabi % ${#MATCHES} + 1 )); ans=${MATCHES[tabi]}
          fi
        fi
        continue ;;
      $'\n'|$'\r') print -n $'\e[?25l'; dirty_footer; REPLY=$ans; return 0 ;;
      $'\e')
        # An arrow key arrives as esc [ A, so a lone esc is the one that means
        # back out; anything following it is a sequence to swallow.
        if read -s -k1 -t 0.05 seq; then
          [[ $seq == '[' || $seq == O ]] && read -s -k1 -t 0.05 seq
          continue
        fi
        break ;;
      $'\x07') break ;;
      $'\x7f'|$'\b') ans="${ans[1,-2]}"; stem=$ans; tabi=0 ;;
      $'\x15') ans=""; stem=""; tabi=0 ;;
      [[:print:]]) ans+=$k; stem=$ans; tabi=0 ;;
    esac
  done
  print -n $'\e[?25l'
  dirty_footer
  REPLY=""
  return 1
}

confirm() {
  local k q=$1
  (( ${#q} > COLUMNS - 4 )) && q="${q[1,COLUMNS-5]}…"
  print -n $'\e['"$(( LINES - 1 ))"';1H'$'\e[K'"${C_RED}  ${q}${RS}"
  print -n $'\e['"$LINES"';1H'$'\e[K'"${C_TX}  y removes it${C_FAINT}  ·  any other key cancels${RS}"
  read -s -k1 k
  dirty_footer
  [[ $k == y || $k == Y ]]
}

# Move by section rather than by row. j and k are fine for a handful of
# conversations and useless once there are screens of them, and the sections are
# already the thing the eye navigates by - whatever the current order groups on,
# not just topics.
section_jump() {   # section_jump <-1|1>
  (( NROWS )) || return
  local dir=$1 i s0
  section_of $CUR; s0=$SECT
  if (( dir > 0 )); then
    for (( i=CUR+1; i<=NROWS; i++ )); do
      section_of $i
      [[ $SECT == $s0 ]] || { CUR=$i; break }
    done
  else
    for (( i=CUR-1; i>=1; i-- )); do
      section_of $i
      [[ $SECT == $s0 ]] || break
    done
    if (( i >= 1 )); then
      # Sitting on a section's first row already: skip past the whole section
      # above rather than landing on its last row, which is what { does in vim.
      if (( i == CUR - 1 )); then
        s0=$SECT
        for (( ; i>=1; i-- )); do
          section_of $i
          [[ $SECT == $s0 ]] || break
        done
      fi
      CUR=$(( i + 1 ))
    else
      CUR=1
    fi
  fi
  SEL_ID=${r_id[$CUR]:-}; PEND_AT=$EPOCHREALTIME
}

# actions -------------------------------------------------------------------

is_thread() { [[ -n ${r_id[$CUR]:-} ]] }
no_thread()  { note "that is an empty topic — n starts a conversation in it" }

# The window has a key bar too now, so "the other pane" is no longer a safe
# guess: the launcher records which one the conversation goes in.
stage_pane() {
  local st=$(tmux show -v @stage_pane 2>/dev/null)
  [[ -n $st ]] && { print -r -- "$st"; return }
  tmux list-panes -F '#{pane_id}' 2>/dev/null | grep -v "^${TMUX_PANE}\$" | head -1
}

show_row() {  # show_row <focus 0|1> — resumes a parked conversation
  (( NROWS == 0 )) && return
  is_thread || { no_thread; return }
  local id=$r_id[$CUR] st=$(stage_pane)
  [[ -z $st ]] && { note "no room for a conversation pane"; return }
  [[ $r_cold[$CUR] == 1 ]] && note "resuming ${r_label[$CUR]}…"
  "$HOST" start "$id" >/dev/null 2>&1
  tmux respawn-pane -k -t "$st" \
    "unset TMUX; tmux attach -t '=$id' 2>/dev/null; exec '$STAGE'" 2>/dev/null
  [[ $1 == 1 ]] && tmux select-pane -t "$st"
  SHOWN="${id}:0"; SHOWN_AT=$EPOCHREALTIME
  "$HOST" seen "$id" 2>/dev/null
  SEEN_MARKED=$id
  MSG=""
  refresh_now; load; build; draw
}

# The right pane trails the cursor once it settles. It never calls `start`: a
# scroll past twelve parked conversations must not launch twelve of them, so a
# parked row gets a placeholder and waits for enter.
follow_tick() {
  (( FOLLOW )) || return
  (( NROWS == 0 )) && return
  local want="${r_id[$CUR]}:${r_cold[$CUR]}"
  [[ $want == $SHOWN ]] && return
  (( EPOCHREALTIME - PEND_AT < 0.12 )) && return
  local st=$(stage_pane)
  [[ -z $st ]] && return
  if ! is_thread; then
    tmux respawn-pane -k -t "$st" "'$STAGE' --empty \"${r_topic[$CUR]}\"" 2>/dev/null
    SHOWN=$want
    return
  fi
  if [[ $r_cold[$CUR] == 1 ]]; then
    tmux respawn-pane -k -t "$st" "'$STAGE' --parked \"$r_label[$CUR]\"" 2>/dev/null
  else
    tmux respawn-pane -k -t "$st" \
      "unset TMUX; tmux attach -t '=${r_id[$CUR]}' 2>/dev/null; exec '$STAGE'" 2>/dev/null
  fi
  SHOWN=$want; SHOWN_AT=$EPOCHREALTIME; SEEN_MARKED=""
}

# Parking is not closing: the conversation keeps its place in the list, its
# recap and its transcript, and enter picks it straight back up. All that goes
# away is the process — which is the point, since each one holds a Claude and
# four-plus MCP servers open for as long as it lives.
reap_tick() {
  (( REAP_HOURS > 0 )) || return
  (( EPOCHSECONDS - REAPED_AT < 300 )) && return
  REAPED_AT=$EPOCHSECONDS
  local shown=${SHOWN%%:*} out
  out=$("$HOST" reap "$REAP_HOURS" "$shown" 2>/dev/null)
  [[ -z $out ]] && return
  refresh_now; load; build
  note "parked ${#${(f)out}} idle over ${REAP_HOURS}h — enter resumes any of them"
}

# Following puts a conversation on screen as you scroll, which is not reading
# it. Only a conversation you have left up for a few seconds counts.
mark_seen_tick() {
  (( NROWS )) || return
  local id=${r_id[$CUR]:-}
  [[ -z $id || $id == $SEEN_MARKED ]] && return
  [[ "${id}:${r_cold[$CUR]}" == $SHOWN ]] || return
  (( EPOCHREALTIME - SHOWN_AT < 3 )) && return
  "$HOST" seen "$id" 2>/dev/null
  SEEN_MARKED=$id
  refresh_now; load; build; draw
}

pick_import() {
  local raw; raw=$("$HOST" scan 60 2>/dev/null)
  local n=$(print -r -- "$raw" | jq 'length' 2>/dev/null)
  [[ -z $n || $n == 0 ]] && { note "nothing unattached in the last 60 days"; return }
  local -a p_sid p_title p_age
  local sid title upd
  while IFS=$SEP read -r sid title upd; do
    p_sid+=("$sid"); p_title+=("$title"); p_age+=("$(( (EPOCHSECONDS - upd) / 86400 ))d")
  done < <(print -r -- "$raw" | jq -r --arg s "$SEP" '.[] | [.claude_session,.title,(.updated_at|tostring)] | join($s)')

  local sel=1 k top=1
  while true; do
    local -a t; t=( "" "  ${C_TX}Bring back which conversation?${RS}" "" )
    local i=0
    (( sel < top )) && top=$sel
    (( sel > top + LINES - 8 )) && top=$(( sel - LINES + 8 ))
    for (( i=top; i<=${#p_sid} && i < top + LINES - 7; i++ )); do
      if (( i == sel )); then
        t+=( "${BG_SEL}  ${C_YEL}${(l:4:)p_age[$i]}  ${C_TX}${p_title[$i][1,COLUMNS-10]}"$'\e[K'"${RS}" )
      else
        t+=( "  ${C_FAINT}${(l:4:)p_age[$i]}  ${C_MUT}${p_title[$i][1,COLUMNS-10]}${RS}" )
      fi
    done
    t+=( "" "  ${C_FAINT}j k move   enter choose   esc cancel${RS}" )
    local buf=$'\e[H' j
    for (( j=1; j<=LINES; j++ )); do
      buf+="${t[$j]:-}"$'\e[K'; (( j < LINES )) && buf+=$'\r\n'
    done
    print -n -- "$buf"
    read -s -k1 k
    case $k in
      j) (( sel < ${#p_sid} )) && (( sel++ )) ;;
      k) (( sel > 1 )) && (( sel-- )) ;;
      $'\n'|$'\r')
        ask "Topic for \"${p_title[$sel][1,30]}\"" || { MSG=""; continue }
        [[ -z $REPLY ]] && continue
        SEL_ID=$("$HOST" register "$p_sid[$sel]" "$REPLY" "$p_title[$sel]")
        PREV=(); refresh_now; load; build; note "brought back"
        return ;;
      q|$'\e') PREV=(); return ;;
    esac
  done
}

# Most rows are parked on one line of input from you. Walking over to the pane
# to type five words is the whole babysitting problem, so type them here.
reply_to() {
  (( NROWS )) || return
  is_thread || { no_thread; return }
  local id=$r_id[$CUR] lab=$r_label[$CUR]
  ask "Reply to \"${lab[1,26]}\"" || { MSG=""; return }
  [[ -z $REPLY ]] && { MSG=""; return }
  local text=$REPLY
  # Sent in the background: a parked conversation has to boot first, and the
  # list should stay usable while it does.
  ( "$HOST" reply "$id" "$text" >/dev/null 2>&1 & )
  if [[ $r_cold[$CUR] == 1 ]]; then
    note "resuming ${lab[1,24]} — reply lands once it is up"
  else
    note "sent to ${lab[1,30]}"
  fi
}

# A full-screen read of where the tokens and the hours went. `q` returns.
usage_view() {
  local st=$(stage_pane)
  [[ -z $st ]] && { note "no room to show it"; return }
  tmux respawn-pane -k -t "$st" \
    "'$HOST' usage | less -R -S; exec '$STAGE'" 2>/dev/null
  tmux select-pane -t "$st"
  SHOWN=""
  note "usage report — q closes it"
}

new_topic() {
  # Completing here too, so a topic that already exists gets reused instead of
  # gaining a near-duplicate that differs by a capital letter.
  set_topic_cand
  ask "New topic" || { MSG=""; return }
  local t=$REPLY
  [[ -z $t ]] && return
  "$HOST" topic-add "$t"
  SEL_ID=""; ORDER=topic
  refresh_now; load
  # Land on the new topic so `n` starts its first conversation right there.
  local i=0
  for (( i=1; i<=NROWS; i++ )); do
    [[ $r_topic[i] == $t ]] && { CUR=$i; SEL_ID=$r_id[$i]; break }
  done
  build; note "topic \"$t\" — n starts a conversation in it"
}

new_thread() {
  # No topic required up front. Sitting on one adopts it, because that is
  # plainly what you meant; anywhere else the conversation starts unfiled and
  # T files it once you can see what it became.
  local t=""
  (( NROWS )) && is_thread && t=$r_topic[$CUR]
  ask "New conversation" || { MSG=""; return }
  local l=${REPLY:-new conversation}
  # Offered, not required: enter keeps the topic you were sitting on, and
  # clearing it leaves the conversation unfiled until T files it.
  # No default shown: a value in the prompt reads as "enter keeps this", and
  # here an empty answer deliberately means unfiled. Tab completes what exists.
  ASK_CAND=( ${(f)"$("$HOST" topics 2>/dev/null)"} )
  ask "Topic (blank leaves it unfiled)" || { MSG=""; return }
  t=$REPLY
  note "starting…"; SEL_ID=$("$HOST" new "$t" "$l")
  enter_new
}

# Creating a conversation should drop you into it. Without this the cursor
# landed wherever the new row sorted to and you had to go find it.
enter_new() {
  refresh_now; load; build; MSG=""; draw
  [[ -n ${SEL_ID:-} && ${r_id[$CUR]:-} == $SEL_ID ]] && show_row 1
}

# Type to narrow, enter to land on it. Nothing is started until you land: the
# filter only moves the cursor, and the right pane follows as it always does.
filter_mode() {
  local k seq
  FILTER=""
  while :; do
    snap_match; build; draw
    print -n $'\e['"$LINES"';1H'$'\e[K'"${C_TOPIC}  /${C_TX}${FILTER}"\
"${C_FAINT}   enter opens  ·  ctrl-n/p next  ·  esc cancels${RS}"$'\e[?25h'
    dirty_footer
    read -s -k1 k || break
    print -n $'\e[?25l'
    case $k in
      $'\n'|$'\r')
        FILTER=""; build; draw
        (( NROWS )) && show_row 1
        return ;;
      $'\e')
        if read -s -k1 -t 0.05 seq; then
          [[ $seq == '[' || $seq == O ]] && read -s -k1 -t 0.05 seq
          case $seq in (A) snap_match -1 ;; (B) snap_match 1 ;; esac
          continue
        fi
        break ;;
      $'\x0e') snap_match 1 ;;
      $'\x10') snap_match -1 ;;
      $'\x7f'|$'\b') FILTER="${FILTER[1,-2]}" ;;
      $'\x15') FILTER="" ;;
      [[:print:]]) FILTER+=$k ;;
    esac
  done
  print -n $'\e[?25l'
  FILTER=""; snap_match; build; draw
}

# main ----------------------------------------------------------------------

# `read -s` only covers the instant it is reading. A key pressed while the panel
# is busy — refreshing, drawing — is echoed by the tty driver and lands in the
# middle of a row. Take echo off the pane for as long as the panel owns it.
teardown() { stty echo 2>/dev/null; print -n $'\e[?25h\e[?1049l' }
trap 'kill $REFRESHER 2>/dev/null; teardown' EXIT INT TERM
# The trap only raises a flag. Rebuilding inside it caught `load` midway through
# emptying the row arrays and drew an empty list that nothing then corrected.
RESIZED=0
TRAPWINCH() { RESIZED=1 }

winsize
stty -echo 2>/dev/null
print -n $'\e[?1049h\e[?25l'
MSG="  loading…"; print -n $'\e[H'"${C_FAINT}  loading…${RS}"
refresh_now
{ while sleep 4; do refresh_now; done } &
REFRESHER=$!
MSG=""
load; build; DIRTY=0; draw

STAMP=0
while true; do
  key=""
  # A short wait while the pane owes the cursor a conversation, so following
  # feels immediate; a long one otherwise, so an idle cockpit stays cheap.
  waitfor=1
  (( FOLLOW && NROWS )) && [[ "${r_id[$CUR]}:${r_cold[$CUR]}" != $SHOWN ]] && waitfor=0.04
  read -s -t $waitfor -k1 key
  if [[ -z $key ]]; then
    if (( RESIZED )); then RESIZED=0; winsize; PREV=(); build; draw; fi
    follow_tick
    mark_seen_tick
    reap_tick
    # Wait for the file to stop changing and to parse before re-exec'ing it.
    # Reloading mid-write ran a half-written script, which left the panel
    # spewing into the pane instead of drawing.
    local mt=$(stat -f %m "$SELF" 2>/dev/null)
    if [[ -n $mt && $mt != $SELF_MTIME ]]; then
      if [[ $mt == ${SETTLING:-} ]]; then
        if zsh -n "$SELF" 2>/dev/null; then
          reload
        else
          SELF_MTIME=$mt; SETTLING=""; note "cockpit script does not parse — not reloading"
        fi
      else
        SETTLING=$mt
      fi
    fi
    local now=$(stat -f %m "$CACHE" 2>/dev/null)
    if [[ $now != $STAMP ]]; then STAMP=$now; load; build; DIRTY=0; draw; fi
    continue
  fi
  if [[ $key == $'\e' ]]; then
    local rest=""; read -s -t 0.12 -k2 rest 2>/dev/null
    case $rest in ('[A') key=k ;; ('[B') key=j ;; ('[C') key=$'\n' ;; (*) key="" ;; esac
  fi
  MSG=""
  case $key in
    j) (( NROWS )) && { (( CUR < NROWS )) && (( CUR++ )) || CUR=1 }
       SEL_ID=${r_id[$CUR]:-}; PEND_AT=$EPOCHREALTIME ;;
    k) (( NROWS )) && { (( CUR > 1 )) && (( CUR-- )) || CUR=$NROWS }
       SEL_ID=${r_id[$CUR]:-}; PEND_AT=$EPOCHREALTIME ;;
    '}'|']') section_jump 1 ;;
    '{'|'[') section_jump -1 ;;
    g) CUR=1; SEL_ID=${r_id[$CUR]:-}; PEND_AT=$EPOCHREALTIME ;;
    G) CUR=$NROWS; SEL_ID=${r_id[$CUR]:-}; PEND_AT=$EPOCHREALTIME ;;
    $'\n'|$'\r') show_row 1 ;;
    $'\t')       show_row 0 ;;
    f) FOLLOW=$(( 1 - FOLLOW )); PEND_AT=0
       (( FOLLOW )) && note "right pane follows the cursor" \
                    || note "right pane stays put — tab shows one" ;;
    o) local i=${ORDERS[(i)$ORDER]}; ORDER=${ORDERS[$(( i % ${#ORDERS} + 1 ))]}; load ;;
    z) FOLDED=$(( 1 - FOLDED )); DIRTY=1 ;;
    R) note "refreshing…"; refresh_now; load ;;
    $'\x0c') PREV=(); print -n $'\e[2J'; draw ;;
    /) filter_mode ;;
    a) reply_to ;;
    u) usage_view ;;
    t) new_topic ;;
    n) new_thread ;;
    i) pick_import ;;
    r) (( NROWS )) && is_thread && { if ask "Label" "$r_label[$CUR]" && [[ -n $REPLY ]]; then
         "$HOST" label "$r_id[$CUR]" "$REPLY"; refresh_now; load; else MSG=""; fi } ;;
    T) (( NROWS )) && is_thread && { set_topic_cand; if ask "Topic" "$r_topic[$CUR]" && [[ -n $REPLY ]]; then
         "$HOST" topic "$r_id[$CUR]" "$REPLY"; refresh_now; load; else MSG=""; fi } ;;
    x) (( NROWS )) && is_thread && {
         if [[ $r_cold[$CUR] == 1 ]]; then note "already parked"
         else "$HOST" stop "$r_id[$CUR]"; refresh_now; load; note "parked — enter resumes it"; fi } ;;
    d) (( NROWS )) && is_thread && {
         if confirm "Remove \"${r_label[$CUR]}\" for good?"; then
           "$HOST" forget "$r_id[$CUR]"; refresh_now; load; note "removed"
         else MSG=""; fi } ;;
    D) (( NROWS )) && {
         local tp=$r_topic[$CUR] i n=0
         for (( i=1; i<=NROWS; i++ )); do
           [[ $r_topic[i] == $tp && -n $r_id[i] ]] && (( n++ ))
         done
         if confirm "Remove topic \"$tp\" and its ${n} conversations?"; then
           for (( i=1; i<=NROWS; i++ )); do
             [[ $r_topic[i] == $tp && -n $r_id[i] ]] && "$HOST" forget "$r_id[$i]"
           done
           "$HOST" topic-rm "$tp"
           SEL_ID=""; refresh_now; load; note "removed"
         else MSG=""; fi } ;;
    '<'|'>')
      local w=$(tmux show -t cockpit -v @list_width 2>/dev/null); w=${w:-54}
      [[ $key == '>' ]] && (( w += 4 )) || (( w -= 4 ))
      (( w < 30 )) && w=30
      tmux set -t cockpit @list_width "$w"
      tmux resize-pane -t "$TMUX_PANE" -x "$w"
      DIRTY=1
      ;;
    $'\C-r') note "reloading…"; reload ;;
    q) tmux switch-client -l 2>/dev/null || tmux detach-client; ;;
  esac
  # Moving the cursor only changes which row is highlighted; rebuilding the
  # whole layout for that is what a keystroke cannot afford.
  (( DIRTY )) && { build; DIRTY=0 }
  draw
done
