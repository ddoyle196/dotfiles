#!/bin/bash
# Writes the one-line "where we left off" plus the state the cockpit renders.
# Both come from one model call so the badge and the sentence cannot disagree.
set -u
sid="$1"; transcript="$2"
idx="$HOME/.claude/session-index"
out="$idx/$sid.json"
lock="$idx/$sid.lock"

mkdir "$lock" 2>/dev/null || exit 0
trap 'rmdir "$lock" 2>/dev/null' EXIT
[ -r "$transcript" ] || exit 0

msgs=$(tail -500 "$transcript" | jq -r '
  select(.type=="user" or .type=="assistant")
  | (.message.content // empty) as $c
  | (if ($c|type)=="string" then $c
     else ([$c[]? | select(.type=="text") | .text] | join(" ")) end) as $t
  | select($t != null and ($t|length) > 0)
  | "\(.type|ascii_upcase): \($t|gsub("\\s+";" "))"' 2>/dev/null | grep -v '^USER: <')

[ -z "$msgs" ] && exit 0

# Earlier turns are context and get clipped from the front. The final turn is
# clipped from the BACK, because a closing question is the whole signal and it
# lives in the last characters of the message.
context=$(printf '%s\n' "$msgs" | tail -8 | head -7 | cut -c1-300)
final=$(printf '%s\n' "$msgs" | tail -1 | rev | cut -c1-1200 | rev)

prompt=$(printf 'Here is the tail of a work conversation between a person and an AI assistant.

<<<TRANSCRIPT
%s
LAST MESSAGE >> %s
TRANSCRIPT

Do not continue that conversation. You are classifying it from the outside, and
the LAST MESSAGE decides most of it.

Reply with exactly two lines, nothing before or after:

STATE: one of answer, pickup, waiting, done, running
  answer  = the LAST MESSAGE puts an explicit, unanswered question or choice to
            the person. There is a question mark aimed at them, or a direct ask
            such as "should I", "do you want", "which one", "confirm", "let me
            know". Recommending something, reporting findings, or ending on
            advice is NOT answer, however consequential it sounds: an implied
            decision is not a question. When in doubt, choose pickup.
  pickup  = the default. Work sits with the person to continue, but nothing
            is being asked of them right now
  waiting = unfinished, but the next move belongs to another person or team
            named in the conversation
  done    = the thread itself is over. The person signed off, or said the work
            is complete and asked for nothing further. Be reluctant here.
            An assistant reporting that it finished a task is NOT done: in a
            working conversation that is just a checkpoint, and the person will
            usually reply with the next thing. Prefer pickup unless the person
            has actually closed the thread.
  running = the assistant is mid-task right now
RECAP: one sentence, max 20 words, plain non-technical English, naming the next
step. If nothing is left to do, say so plainly. No markdown, no quotes.
' "$context" "$final")

raw=$(printf '%s' "$prompt" | CC_RECAP_CHILD=1 claude -p \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --max-turns 1 \
  --model claude-haiku-4-5-20251001 \
  2>/dev/null)

state=$(printf '%s' "$raw" | sed -n 's/^[[:space:]]*STATE:[[:space:]]*//p' \
  | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z' | head -c 12)
recap=$(printf '%s' "$raw" | sed -n 's/^[[:space:]]*RECAP:[[:space:]]*//p' \
  | tr '\n' ' ' | sed 's/  */ /g; s/^ //; s/ $//')

case "$state" in
  answer|pickup|waiting|done|running) ;;
  *) state="pickup" ;;   # unparseable: assume it is yours, never hide it
esac

# The model called 11 of 28 conversations "answer" and 6 of those asked nothing
# at all, which made red mean nothing. "Someone asked you something" is visible
# in the text, so decide it in the text rather than trusting the classifier.
ask_re='[?]|(should|shall) i|do you want|would you like|which (one|of|approach|option)|let me know|your call|want me to|say the word|confirm|approve|sign off|pick one|which would you'
if printf '%s' "$final" | tail -c 400 | grep -qiE "$ask_re"; then
  [ "$state" = pickup ] && state=answer
else
  [ "$state" = answer ] && state=pickup
fi
[ -z "$recap" ] && exit 0

lines=$(wc -l < "$transcript" | tr -d ' ')
jq -n --arg s "$sid" --arg r "$recap" --arg st "$state" --arg t "$transcript" \
      --argjson l "$lines" --argjson u "$(date +%s)" \
  '{session_id:$s, recap:$r, state:$st, transcript:$t, lines:$l, updated_at:$u}' \
  > "$out.tmp" && mv "$out.tmp" "$out"
