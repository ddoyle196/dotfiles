#!/usr/bin/env bash
# Drop cockpit sessions from the tmux-resurrect snapshot.
#
# The cockpit owns the lifecycle of every cc_* session: a conversation is a
# registry entry, and `cc-host.sh start` is what puts a process behind it.
# Resurrect captured those panes' *child* command (an mcp server, say) rather
# than claude, so after a reboot it restored the session NAME with a bare shell
# — a husk that looks alive and drops you at a prompt. Leaving them out means a
# reboot returns them as parked, which is what they actually are.
set -uo pipefail
f=$(readlink "$HOME/.tmux/resurrect/last" 2>/dev/null) || exit 0
[ -n "$f" ] || exit 0
case $f in /*) ;; *) f="$HOME/.tmux/resurrect/$f" ;; esac
[ -f "$f" ] || exit 0

python3 - "$f" <<'PY'
import sys, os
path = sys.argv[1]
# A pane's saved command can contain newlines, so a record is its header line
# plus every line before the next header: dropping only the header would leave
# the remainder behind as junk.
HEADERS = ("pane\t", "window\t", "state\t", "grouped_session\t")
out, drop = [], False
for line in open(path):
    if line.startswith(HEADERS):
        drop = line.split("\t", 2)[1].startswith("cc_") if line.count("\t") > 1 else False
    if not drop:
        out.append(line)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.writelines(out)
os.replace(tmp, path)
PY
