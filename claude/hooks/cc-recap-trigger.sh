#!/bin/bash
# Stop hook: keeps the session's plain-English recap reasonably fresh.
# Debounced and fully detached, so it never delays a turn.
set -u
[ -n "${CC_RECAP_CHILD:-}" ] && exit 0

input=$(cat)
sid=$(printf '%s' "$input" | jq -r '.session_id // empty')
transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty')
[ -z "$sid" ] || [ -z "$transcript" ] && exit 0
[ -r "$transcript" ] || exit 0

out="$HOME/.claude/session-index/$sid.json"
now=$(date +%s)
lines=$(wc -l < "$transcript" | tr -d ' ')

if [ -f "$out" ]; then
  prev_at=$(jq -r '.updated_at // 0' "$out" 2>/dev/null)
  prev_lines=$(jq -r '.lines // 0' "$out" 2>/dev/null)
  age=$(( now - prev_at ))
  grown=$(( lines - prev_lines ))
  # Under two minutes old and no large jump: the cached line is still true enough.
  [ "$age" -lt 120 ] && [ "$grown" -lt 200 ] && exit 0
  [ "$grown" -le 0 ] && exit 0
fi

nohup "$HOME/.claude/hooks/cc-recap-gen.sh" "$sid" "$transcript" >/dev/null 2>&1 &
exit 0
