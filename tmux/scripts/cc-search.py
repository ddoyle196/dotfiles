#!/usr/bin/env python3
"""Search what was said, not just what it was called.

The index cc-harvest.py keeps is one file of prose per conversation, so this is a
plain substring scan over a few megabytes -- fast enough to run on a keystroke,
and with no dependency the cockpit did not already have. Substring, not the
subsequence match `/` uses on labels: over a megabyte of prose a subsequence
matches nearly everything.

Removed conversations are searched too and marked as such, because knowing you
discussed something months ago is exactly when search is worth having. The panel
offers to restore what it finds.

  cc-search.py <reg> <query>   -> cc_id \\x01 live \\x01 topic \\x01 label \\x01 updated \\x01 line
"""
import json
import os
import sys

HOME = os.path.expanduser("~")
COCKPIT = os.path.join(HOME, ".claude", "cockpit")
TEXT = os.path.join(COCKPIT, "text")
GONE = os.path.join(COCKPIT, "removed")
SEP = "\x01"
# Enough of the line to recognise the moment, short enough for one row.
SNIP = 160
# Keep the match itself away from the ragged left edge, so what you typed is
# never the first thing cut off.
LEAD = 30


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def rows(reg_dir):
    """sid -> what the panel needs to draw the conversation it belongs to."""
    out = {}
    for d, live in ((reg_dir, 1), (GONE, 0)):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not (name.startswith("cc_") and name.endswith(".json")):
                continue
            rec = load(os.path.join(d, name)) or {}
            sid = rec.get("claude_session") or ""
            # A live row wins: the same transcript can sit in `removed` from an
            # earlier pass, and the one you can still open is the useful answer.
            if sid and (live or sid not in out):
                out[sid] = (name[:-5], live, rec.get("topic") or "",
                            rec.get("label") or "", int(rec.get("updated_at") or 0))
    return out


def snippet(line, at, needle):
    """The matching line, trimmed around the match rather than from the start."""
    start = max(0, at - LEAD)
    end = start + SNIP
    text = line[start:end]
    if start:
        text = "…" + text
    if end < len(line):
        text += "…"
    return text


def search(reg_dir, query):
    needle = query.lower()
    found = []
    for sid, meta in rows(reg_dir).items():
        path = os.path.join(TEXT, sid + ".txt")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            # The whole file first: one pass says whether to bother with lines at
            # all, and most conversations do not match.
            low = body.lower()
            if needle not in low:
                continue
            best = None
            hits = low.count(needle)
            for line in body.splitlines():
                at = line.lower().find(needle)
                if at < 0:
                    continue
                # Your own words outrank Claude's for finding a conversation
                # again: you remember what you asked, not how it was answered.
                mine = line.startswith("you:")
                if best is None or (mine and not best[0]):
                    best = (mine, snippet(line, at, needle))
                if mine:
                    break
        except OSError:
            continue
        if best:
            found.append((meta, best[1], hits, best[0]))
    # Most relevant first: your own words, then how often it came up, then which
    # conversation moved most recently.
    found.sort(key=lambda f: (not f[3], -f[2], -f[0][4]))
    return found


def main() -> int:
    if len(sys.argv) < 3:
        sys.exit("usage: cc-search.py <reg> <query>")
    reg_dir, query = sys.argv[1], sys.argv[2]
    if not query.strip():
        return 0
    for meta, line, _hits, _mine in search(reg_dir, query):
        cid, live, topic, label, upd = meta
        print(SEP.join([cid, str(live), topic, label, str(upd), line]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
