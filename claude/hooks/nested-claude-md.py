#!/usr/bin/env python3
"""Load a subdirectory's CLAUDE.md when work actually reaches that subdirectory.

Claude Code reads CLAUDE.md at the session's cwd and above it, but not below.
Starting a session at a parent directory and then editing a subproject therefore
loses that subproject's conventions entirely.

This walks up from the path a tool is about to touch, stopping at the session
cwd, and injects the nearest CLAUDE.md it finds strictly below cwd. Nothing is
hard-coded to a project, and it fires only when a subproject is actually reached
- so a session that never leaves the parent pays nothing for this.

Injected once per file per session: the context persists in the transcript, and
re-sending it on every read would crowd out the conversation it is meant to help.
"""
import hashlib
import json
import os
import sys

MAX_BYTES = 32_768          # a CLAUDE.md larger than this is pathological
STATE_DIR = os.path.expanduser("~/.claude/cache/nested-claude-md")

# Where each tool keeps the thing it is about to touch.
PATH_KEYS = ("file_path", "path", "notebook_path")
MAX_TOKENS = 60             # a shell one-liner has no business being longer


def candidates(ti):
    """Every path a tool call might reach, best guess first.

    Bash matters as much as Read here: under --dangerously-skip-permissions the
    agent prefers `cat` over the Read tool, which is exactly how the cockpit
    runs, so a file-tool-only hook would never fire where it is needed most.
    """
    for k in PATH_KEYS:
        v = ti.get(k)
        if isinstance(v, str) and v:
            yield v
    cmd = ti.get("command")
    if isinstance(cmd, str):
        for tok in cmd.replace("&&", " ").replace("|", " ").split()[:MAX_TOKENS]:
            tok = tok.strip("\"'`()<>;,")
            # Anything that survives and exists is a real path; testing is
            # cheaper and far more reliable than guessing at shell grammar.
            if tok and not tok.startswith("-"):
                yield tok


def bail():
    sys.exit(0)              # never obstruct the tool call


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        bail()

    cwd = os.path.realpath(ev.get("cwd") or os.getcwd())
    ti = ev.get("tool_input") or {}

    found = None
    for raw in candidates(ti):
        target = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(cwd, raw))
        # Only look inside the session's own tree; anything else is not ours.
        if not target.startswith(cwd + os.sep):
            continue
        if not os.path.exists(target):
            continue
        # Climb from the path towards cwd, stopping before cwd itself, whose
        # own CLAUDE.md is already loaded natively.
        d = target if os.path.isdir(target) else os.path.dirname(target)
        while d.startswith(cwd + os.sep):
            c = os.path.join(d, "CLAUDE.md")
            if os.path.isfile(c):
                found = c
                break
            d = os.path.dirname(d)
        if found:
            break
    if not found:
        bail()

    sid = ev.get("session_id") or "nosession"
    key = hashlib.sha256(f"{sid}\0{found}".encode()).hexdigest()[:20]
    stamp = os.path.join(STATE_DIR, key)
    if os.path.exists(stamp):
        bail()

    try:
        with open(found, encoding="utf-8", errors="replace") as fh:
            body = fh.read(MAX_BYTES + 1)
    except OSError:
        bail()
    truncated = len(body) > MAX_BYTES
    body = body[:MAX_BYTES]

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        open(stamp, "w").close()
    except OSError:
        pass                 # a missing stamp costs a repeat, not a failure

    rel = os.path.relpath(found, cwd)
    note = "\n\n[truncated]" if truncated else ""
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        # Deliberately no permissionDecision: this hook adds context and must
        # not become a blanket approval for every file the agent touches.
        "additionalContext":
            f"Project instructions from {rel}, loaded because this tool call "
            f"touches that subproject. They apply to work under "
            f"{os.path.dirname(rel) or '.'}/ and take precedence over the "
            f"parent directory's conventions there.\n\n{body}{note}",
    }}, sys.stdout)


if __name__ == "__main__":
    main()
