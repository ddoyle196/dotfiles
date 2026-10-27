#!/bin/bash
# Writes the one-line "where we left off" plus the state the cockpit renders.
# Both come from one model call so the badge and the sentence cannot disagree.
set -u
sid="$1"; transcript="$2"
idx="$HOME/.claude/session-index"
out="$idx/$sid.json"
lock="$idx/$sid.lock"

# The index directory is this script's to create: nothing else makes it, and
# without it the lock below fails and every recap exits silently.
mkdir -p "$idx" 2>/dev/null
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

Reply with exactly three lines, nothing before or after:

STATE: one of answer, pickup, waiting, done
  answer  = the LAST MESSAGE puts an explicit, unanswered question or choice to
            the person. There is a question mark aimed at them, or a direct ask
            such as "should I", "do you want", "which one", "confirm", "let me
            know". Recommending something, reporting findings, or ending on
            advice is NOT answer, however consequential it sounds: an implied
            decision is not a question. When in doubt, choose pickup.
  pickup  = the default. Work sits with the person to continue, but nothing
            is being asked of them right now
  waiting = unfinished, but the next move belongs to another PERSON or team
            named in the conversation. Subagents the assistant started,
            background jobs or running commands are not someone else: work
            the assistant started and is waiting on is pickup, not waiting.
  done    = the thread itself is over. The person signed off, or said the work
            is complete and asked for nothing further. Be reluctant here.
            An assistant reporting that it finished a task is NOT done: in a
            working conversation that is just a checkpoint, and the person will
            usually reply with the next thing. Prefer pickup unless the person
            has actually closed the thread.
RECAP: one sentence, max 20 words, plain non-technical English, naming the next
step. If nothing is left to do, say so plainly. No markdown, no quotes.
WAITING_ON: the person or team the conversation names as having the next move,
  in a few words. Subagents, background jobs and running commands do not count.
  Write none if no one else holds it.
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
who=$(printf '%s' "$raw" | sed -n 's/^[[:space:]]*WAITING_ON:[[:space:]]*//p' \
  | head -1 | tr -d '"*`' | sed "s/^[_' ]*//; s/[_'[:space:].]*\$//")

case "$state" in
  answer|pickup|waiting|done) ;;
  # Unparseable, or the dropped "running": assume it is yours, never hide it.
  # This hook runs at Stop, so the assistant has by definition just finished and
  # nothing here can be mid-task. A stored "running" was wrong when written and
  # nothing ever cleared it, leaving rows frozen on it for days.
  *) state="pickup" ;;
esac

# "Someone asked you something" is visible in the text, so decide it in the text
# rather than trusting the classifier. Two rules earn most of the accuracy:
#
# Scan the END, not a window. An unanswered question lives in the closing
# sentence; a 400-character window caught "confirmed", "approved" and stray
# question marks from the middle of a report and turned two thirds of the list
# red. Word boundaries matter for the same reason: "confirm" is an ask,
# "confirmed" is a report of one.
# Trailing list items, table rows and code fences are the question's options,
# not its ending, so walk back past them to the sentence that does the asking.
close=$(printf '%s\n' "$final" | sed 's/[[:space:]]*$//' | grep -v '^$' \
  | grep -vE '^[[:space:]]*([-*+>|]|[0-9]+[.)]|`{3})' | tail -1)
ask_re='\?[)"'"'"'`*_]*[[:space:]]*$'
ask_re="$ask_re"'|\b(want me to|shall i|should i|do you want|would you like)\b'
ask_re="$ask_re"'|\b(let me know|say the word|your call|up to you|tell me what|which would you)\b'
# "still open" / "still stands" was tried here and dropped: it fired on "Still
# open from earlier: parking maps aren't loaded" and on "the two questions for
# Darshit still stand", neither of which is a question aimed at you.
if printf '%s' "$close" | grep -qiE "$ask_re"; then
  [ "$state" = pickup ] && state=answer
else
  [ "$state" = answer ] && state=pickup
fi
[ -z "$recap" ] && exit 0

# The overseer shows these beside the row, so each is kept only when the final
# state agrees with it: a name is offered only on a waiting row, and the ask
# only on a row that is asking. A wrong name there is one click from becoming
# a block, which is why the text rules above get the last word, not the model.
# $close comes from a transcript flattened to one line per turn, so list
# options run straight into the question there. The last reply is read again
# with its line breaks, and the same walk-back past list lines finds the line
# that does the asking; the question is its last sentence that asks.
question=""
if [ "$state" = answer ]; then
  last_raw=$(tail -500 "$transcript" | jq -c 'select(.type=="assistant")
    | .message.content
    | if type=="string" then . else ([.[]? | select(.type=="text") | .text] | join("\n")) end
    | select(length > 0)' 2>/dev/null | tail -1 | jq -r '.' 2>/dev/null)
  qline=$(printf '%s\n' "$last_raw" | sed 's/[[:space:]]*$//' | grep -v '^$' \
    | grep -vE '^[[:space:]]*([-*+>|]|[0-9]+[.)]|`{3})' | tail -1)
  [ -n "$qline" ] || qline=$close
  question=$(printf '%s' "$qline" | jq -Rrs --arg re "$ask_re" '
    sub("^ASSISTANT: "; "") | gsub("\\*\\*|__"; "")
    | [splits("(?<=[.?!])\\s+")] | map(select(length > 0))
    | ([.[] | select(test($re; "i"))] | last) // last // ""')
fi
case "$(printf '%s' "$who" | tr '[:upper:]' '[:lower:]')" in
  ''|none|no\ one|nobody|n/a) who="" ;;
esac
[ "$state" = waiting ] || who=""

lines=$(wc -l < "$transcript" | tr -d ' ')
jq -n --arg s "$sid" --arg r "$recap" --arg st "$state" --arg t "$transcript" \
      --arg q "$question" --arg w "$who" \
      --argjson l "$lines" --argjson u "$(date +%s)" \
  '{session_id:$s, recap:$r, state:$st, transcript:$t, lines:$l, updated_at:$u,
    question:($q | gsub("^\\s+|\\s+$"; "")
              | if length > 200 then "..." + .[-197:] else . end),
    waiting_suggest:($w | .[:60])}' \
  > "$out.tmp" && mv "$out.tmp" "$out"
