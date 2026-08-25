#!/bin/zsh
# Process host for the cockpit.
#
# A conversation is a registry file, not a process. The tmux session is only how
# it happens to be running right now; when that dies (crash, reboot, /exit) the
# entry stays and `start` resumes it from its Claude transcript. Everything runs
# on the DEFAULT tmux server, because a pane cannot be moved between servers and
# migrating existing panes has to stay possible.
emulate -L zsh
setopt pipe_fail
zmodload zsh/system
# Hosting always happens on the default server, so never inherit the caller's:
# run from inside any tmux and it would otherwise inspect that one instead.
unset TMUX TMUX_PANE
PREFIX="cc_"
SEP=$'\x01'
# Where conversations are started, and therefore which transcript directory
# holds them. setup.sh records this machine's choice; COCKPIT_DIR overrides it.
# different machine; Claude Code names its transcript folder after the path with
# every slash turned into a dash.
source "$HOME/.tmux/scripts/lib/cc-dir.sh"
CWD=$(_cc_dir)
IDX="$HOME/.claude/session-index"
REG="$HOME/.claude/cockpit/threads"
# Topics are declared, not derived: one can exist with nothing in it yet, the
# way an empty tab used to hold a place for work that had not arrived.
TOPICS="$HOME/.claude/cockpit/topics.json"
PROJ="$HOME/.claude/projects/${CWD//\//-}"
# A conversation remembers the directory it was started in, so resuming puts it
# back where it belongs instead of wherever the cockpit happens to default to.
# Entries written before this existed carry no cwd and fall back to COCKPIT_DIR.
_cwd_for()  { local c; c=$(jq -r '.cwd // ""' "$REG/$1.json" 2>/dev/null); print -r -- "${c:-$CWD}" }
_proj_for() { print -r -- "$HOME/.claude/projects/${1//\//-}" }
# Cockpit conversations run unattended in a background session, where a
# permission prompt is a conversation stuck waiting on a pane nobody is looking
# at. Daniel asked for these to never stop and ask.
CLAUDE_ARGS=( --dangerously-skip-permissions )
mkdir -p "$REG"
[[ -f "$TOPICS" ]] || print '[]' > "$TOPICS"

# Read-modify-write on one shared file: without a lock, two panels declaring
# topics at the same time silently drop each other's addition. mkdir is the
# atomic primitive that needs no module and no pre-existing file.
_topics_edit() {   # _topics_edit <jq program> [jq args...]
  local prog=$1; shift
  local lock="$TOPICS.lock" n=0
  while ! mkdir "$lock" 2>/dev/null; do
    if (( ++n > 250 )); then          # ~5s: assume the holder died
      rm -rf "$lock" 2>/dev/null       # -rf, since an older build left a file here
      n=0
    fi
    sleep 0.02
  done
  jq "$@" "$prog" "$TOPICS" > "$TOPICS.tmp$$" && mv "$TOPICS.tmp$$" "$TOPICS"
  local rc=$?
  rm -rf "$lock" 2>/dev/null
  return $rc
}

_topic_declare() {   # idempotent
  _topics_edit 'if index($t) then . else . + [$t] end' --arg t "$1"
}

_quiet() {  # make a hosted session invisible as a UI
  tmux set -t "$1" status off             2>/dev/null
  tmux set -t "$1" prefix None            2>/dev/null
  tmux set -t "$1" prefix2 None           2>/dev/null
  tmux set -t "$1" destroy-unattached off 2>/dev/null
  tmux set -t "$1" history-limit 50000    2>/dev/null
}

_newid() { print "${PREFIX}$(od -An -tx1 -N4 /dev/urandom | tr -d ' \n')" }

_write() {  # _write <id> <key> <json-value>
  local f="$REG/$1.json"
  [[ -f "$f" ]] || print '{}' > "$f"
  local tmp="$f.tmp$$"
  jq --arg k "$2" --argjson v "$3" '.[$k]=$v' "$f" > "$tmp" && mv "$tmp" "$f"
}

_alive() { tmux has-session -t "=$1" 2>/dev/null }

case "${1:-list}" in

list)
  # One python pass over the registry: a jq per field per file meant ~130
  # processes and well over a second, which is too slow to sit under a key loop.
  {
    print -r -- "AGENTS"
    claude agents --json 2>/dev/null
    print -r -- "PS"
    ps -eo pid=,ppid=
    print -r -- "PANES"
    tmux list-sessions -F "#{session_name} #{pane_pid}" 2>/dev/null | grep "^${PREFIX}"
  } | python3 "$HOME/.tmux/scripts/cc-list.py" "$REG" "$IDX" "$TOPICS"
  # Harvesting is a cheap incremental read; asking GitHub is not, so that runs
  # detached and the list uses whatever answer was last written.
  python3 "$HOME/.tmux/scripts/cc-harvest.py" harvest "$REG" "$PROJ" 2>/dev/null
  ( python3 "$HOME/.tmux/scripts/cc-harvest.py" status >/dev/null 2>&1 & ) 
  ;;

new)
  topic="${2:?topic required}"; label="${3:-new conversation}"
  id=$(_newid)
  _write "$id" topic   "$(jq -Rn --arg v "$topic" '$v')"
  _write "$id" label   "$(jq -Rn --arg v "$label" '$v')"
  _write "$id" created "$(date +%s)"
  _write "$id" cwd     "$(jq -Rn --arg v "$CWD" '$v')"
  _topic_declare "$topic"
  tmux new-session -d -s "$id" -x 200 -y 50 -c "$CWD" claude $CLAUDE_ARGS
  _quiet "$id"
  print "$id"
  ;;

register)  # adopt a transcript that has no process: recovery, and importing history
  sid="${2:?claude session id required}"
  topic="${3:?topic required}"; label="${4:-$sid}"
  id=$(_newid)
  _write "$id" topic          "$(jq -Rn --arg v "$topic" '$v')"
  _write "$id" label          "$(jq -Rn --arg v "$label" '$v')"
  _write "$id" claude_session "\"$sid\""
  _write "$id" created        "$(date +%s)"
  # The transcript records the directory it ran in, which beats deriving it from
  # the folder name: that slug is lossy when a path already contains a dash.
  tcwd=""
  for f in "$HOME/.claude/projects"/*/"$sid".jsonl(N); do
    tcwd=$(grep -m1 -o '"cwd":"[^"]*"' "$f" | cut -d'"' -f4)
    [[ -n "$tcwd" ]] && break
  done
  _write "$id" cwd "$(jq -Rn --arg v "${tcwd:-$CWD}" '$v')"
  _topic_declare "$topic"
  if [[ -f "$IDX/$sid.json" ]]; then
    _write "$id" recap      "$(jq -c '.recap // ""' "$IDX/$sid.json")"
    _write "$id" state      "$(jq -c '.state // "pickup"' "$IDX/$sid.json")"
    _write "$id" updated_at "$(jq -r '.updated_at // 0' "$IDX/$sid.json")"
  elif [[ -f "$PROJ/$sid.jsonl" ]]; then
    _write "$id" updated_at "$(stat -f %m "$PROJ/$sid.jsonl")"
  fi
  print "$id"
  ;;

start)  # bring a parked conversation back up, or no-op if it is already running
  id="${2:?id required}"
  _alive "$id" && { print "$id"; exit 0 }
  [[ -f "$REG/$id.json" ]] || { print -u2 "start: no such conversation $id"; exit 1 }
  sid=$(jq -r '.claude_session // ""' "$REG/$id.json")
  cwd=$(_cwd_for "$id"); proj=$(_proj_for "$cwd")
  if [[ -n "$sid" && -f "$proj/$sid.jsonl" ]]; then
    tmux new-session -d -s "$id" -x 200 -y 50 -c "$cwd" claude $CLAUDE_ARGS --resume "$sid"
  else
    tmux new-session -d -s "$id" -x 200 -y 50 -c "$cwd" claude $CLAUDE_ARGS
  fi
  _quiet "$id"
  print "$id"
  ;;

adopt)  # migration: lift an existing pane into its own hosted session
  pane="${2:?pane id required}"
  # Where the pane actually is, so a parked conversation resumes there and not
  # in whatever directory the cockpit was configured with.
  pcwd=$(tmux display -p -t "$pane" '#{pane_current_path}' 2>/dev/null)
  [[ -n "$pcwd" ]] || pcwd="$CWD"
  topic="${3:-$(tmux display -p -t "$pane" '#{?@topic,#{@topic},#{window_name}}')}"
  label="${4:-$(tmux display -p -t "$pane" '#{?@label,#{@label},#{pane_title}}')}"
  id=$(_newid)
  # break-pane makes it a window of its own; a holding session then donates it.
  tmux break-pane -d -s "$pane" -n "$id" 2>/dev/null || exit 1
  win=$(tmux list-windows -a -F '#{window_id} #{window_name}' | awk -v n="$id" '$2==n{print $1; exit}')
  [[ -z "$win" ]] && { print -u2 "adopt: lost the window after break-pane"; exit 1 }
  tmux new-session -d -s "$id" -c "$pcwd" 'sleep 2147483647'
  tmux move-window -s "$win" -t "$id:" 2>/dev/null || exit 1
  tmux kill-window -t "$id:^" 2>/dev/null   # drop the placeholder
  _quiet "$id"
  _write "$id" topic   "$(jq -Rn --arg v "$topic" '$v')"
  _write "$id" label   "$(jq -Rn --arg v "$label" '$v')"
  _write "$id" created "$(date +%s)"
  _write "$id" cwd     "$(jq -Rn --arg v "$pcwd" '$v')"
  # Same as new/register: an adopted conversation's topic has to be declared, or
  # it exists on the entry but never appears in the topic list.
  _topic_declare "$topic"
  print "$id"
  ;;

scan)  # transcripts with no conversation attached, newest first
  days="${2:-30}"
  typeset -A KNOWN
  for f in "$REG"/${PREFIX}*.json(N); do
    KNOWN[$(jq -r '.claude_session // "-"' "$f")]=1
  done
  python3 - "$PROJ" "$days" "${(kj:,:)KNOWN}" <<'PY'
import json, os, sys, time, glob
proj, days, known = sys.argv[1], int(sys.argv[2]), set(sys.argv[3].split(","))
cut, out = time.time() - days * 86400, []
for f in glob.glob(os.path.join(proj, "*.jsonl")):
    sid = os.path.basename(f)[:-6]
    m = os.path.getmtime(f)
    if m < cut or sid in known:
        continue
    title, turns = None, 0
    with open(f, errors="ignore") as fh:
        for line in fh:
            if '"ai-title"' in line:
                try: title = json.loads(line).get("aiTitle") or title
                except Exception: pass
            elif '"type":"user"' in line:
                turns += 1
    if not title or turns < 2:
        continue
    out.append({"claude_session": sid, "title": title, "updated_at": int(m), "turns": turns})
out.sort(key=lambda r: -r["updated_at"])
print(json.dumps(out))
PY
  ;;

reap)  # stop the processes behind conversations nobody has touched in a while
  hours="${2:-8}"
  cutoff=$(( $(date +%s) - hours * 3600 ))
  keep="${3:-}"          # never reap what is on screen right now
  for f in "$REG"/${PREFIX}*.json(N); do
    id=$(basename "$f" .json)
    [[ "$id" == "$keep" ]] && continue
    _alive "$id" || continue
    # `created` matters: a conversation that has not spoken yet has no
    # updated_at and no last_seen, and without it every new one reads as ancient
    # and got reaped within minutes of being made.
    last=$(jq -r '[.updated_at // 0, .last_seen // 0, .created // 0] | max' "$f")
    (( last > cutoff )) && continue
    # Busy means it is mid-answer; killing that throws the answer away.
    pane=$(tmux list-panes -t "=$id" -F '#{pane_pid}' 2>/dev/null | head -1)
    busy=$(claude agents --json 2>/dev/null | python3 -c "
import json,sys,subprocess
pid=int(sys.argv[1] or 0)
kids={}
for line in subprocess.run(['ps','-eo','pid=,ppid='],capture_output=True,text=True).stdout.split(chr(10)):
    if line.strip():
        p,pp=line.split(); kids.setdefault(int(pp),[]).append(int(p))
want=set([pid]); stack=[pid]
while stack:
    for k in kids.get(stack.pop(),[]):
        want.add(k); stack.append(k)
try: rows=json.load(sys.stdin)
except Exception: rows=[]
rows=rows if isinstance(rows,list) else rows.get('agents',[])
print('1' if any(r.get('pid') in want and r.get('status')=='busy' for r in rows) else '0')
" "$pane" 2>/dev/null)
    [[ "$busy" == 1 ]] && continue
    tmux kill-session -t "=$id" 2>/dev/null
    print -r -- "$id"
  done
  ;;

reply)  # answer a conversation without leaving the list
  id="${2:?id required}"; text="${3:?text required}"
  if ! _alive "$id"; then
    "$0" start "$id" >/dev/null || exit 1
    # Typing into a Claude that has not finished booting drops the text, so give
    # it a moment to come up before sending.
    for i in {1..30}; do
      claude agents --json 2>/dev/null | grep -q '"pid"' && break
      sleep 0.5
    done
    sleep 2
  fi
  # A pane id, not "=session": send-keys will not take an exact-match session
  # target and fails with "can't find pane".
  pane=$(tmux list-panes -t "$id" -F '#{pane_id}' 2>/dev/null | head -1)
  [[ -n $pane ]] || { print -u2 "reply: no pane for $id"; exit 1 }
  tmux send-keys -t "$pane" -l -- "$text"
  sleep 0.3
  tmux send-keys -t "$pane" Enter
  ;;

usage)  # tokens and active time per conversation, grouped by topic; `usage csv` to export
  python3 "$HOME/.tmux/scripts/lib/cc-usage-report.py" \
          "$REG" "$HOME/.claude/cockpit/usage.json" "${2:-}"
  ;;

seen)  _write "${2:?}" last_seen "$(date +%s)" ;;   # you have looked at it since it last spoke

topic) _write "${2:?}" topic "$(jq -Rn --arg v "${3:?}" '$v')"; _topic_declare "${3}" ;;

topics)       jq -r '.[]' "$TOPICS" ;;
topic-add)    _topic_declare "${2:?topic required}" ;;
topic-rm)     _topics_edit 'map(select(. != $t))' --arg t "${2:?}" ;;
topic-rename)
  old="${2:?old name required}"; new="${3:?new name required}"
  for f in "$REG"/${PREFIX}*.json(N); do
    [[ $(jq -r '.topic // ""' "$f") == "$old" ]] || continue
    jq --arg v "$new" '.topic=$v' "$f" > "$f.tmp$$" && mv "$f.tmp$$" "$f"
  done
  _topics_edit 'map(if . == $o then $n else . end) | unique_by(.)' \
               --arg o "$old" --arg n "$new"
  ;;
label) _write "${2:?}" label "$(jq -Rn --arg v "${3:?}" '$v')" ;;
stop)  tmux kill-session -t "=${2:?}" 2>/dev/null; true ;;   # park: process ends, entry stays
forget)                                                       # delete for good
  tmux kill-session -t "=${2:?}" 2>/dev/null
  # Removing is not deleting: the entry moves aside so a mistaken `d` costs
  # nothing. `cc-host.sh restore` puts it back.
  mkdir -p "$REG/../removed"
  [[ -f "$REG/${2}.json" ]] && mv -f "$REG/${2}.json" "$REG/../removed/${2}.json"
  ;;

restore)   # restore [id] — with no id, lists what has been removed
  gone="$REG/../removed"
  if [[ -z "${2:-}" ]]; then
    for f in "$gone"/*.json(N); do
      print -r -- "$(basename $f .json)  $(jq -r '"\(.topic) / \(.label)"' "$f")"
    done
    exit 0
  fi
  [[ -f "$gone/${2}.json" ]] || { print -u2 "restore: no such removed entry"; exit 1 }
  mv -f "$gone/${2}.json" "$REG/${2}.json"
  print "$2"
  ;;

*) print -u2 "usage: cc-host.sh list|new <topic> [label]|register <sid> <topic> [label]|start <id>|adopt <pane> [topic] [label]|scan [days]|topic|label|stop|forget"; exit 2 ;;
esac
