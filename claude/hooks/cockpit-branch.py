#!/usr/bin/env python3
"""Give `/branch` a second row in the cockpit instead of losing the first one.

`/branch` copies the conversation and moves you into the copy, in the same pane.
The cockpit follows that move on its own -- cc-list.py rebinds a row to whatever
session id the live process reports -- so without this the train of thought you
branched away from vanishes from the list. It is still on disk, but finding it
means `scan` and an import, which is exactly the friction that stops you
branching.

So: the pane's row becomes the branch, and the conversation it came from is
registered as a parked row of its own, keeping the label and topic it already
had. Two rows, one gesture, nothing to remember.

Wired to SessionStart with matcher "fork", which is the source `/branch` resumes
under. `/fork` (the background copy) reports the same source, hence the check
that this is running inside a hosted pane.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

THREADS = Path.home() / ".claude" / "cockpit" / "threads"
HOST = Path.home() / ".tmux" / "scripts" / "cc-host.sh"
# Mirrors Claude Code's own `(Branch)` / `(Branch 2)` titles, lowercased to sit
# with the hand-typed labels around it. Also how a title Claude derived on its
# own is told apart from one that was typed: the derived one carries the suffix.
SUFFIX = re.compile(r"\s*\(branch(?: (\d+))?\)$", re.I)
# Long enough for a name worth typing, short enough not to push the state glyphs
# off a narrow list.
MAX_LABEL = 48


def tmux_session() -> str:
    """The cockpit id of the pane this hook is running in, or "" if it is not in one."""
    pane = os.environ.get("TMUX_PANE")
    if not pane or not os.environ.get("TMUX"):
        return ""
    try:
        out = subprocess.run(
            ["tmux", "display", "-p", "-t", pane, "#{session_name}"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    name = out.stdout.strip()
    return name if name.startswith("cc_") else ""


def parent_session(transcript: str, fallback: str, new_sid: str) -> str:
    """The session id this one was branched from.

    Every message the branch copied carries `forkedFrom`, so the transcript is
    the authority. The row's recorded id is only a fallback, and only while it
    still disagrees with the live one: cc-list.py may have rebound it already.
    """
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                sid = (rec.get("forkedFrom") or {}).get("sessionId") or ""
                if sid:
                    return sid
    except OSError:
        pass
    return fallback if fallback and fallback != new_sid else ""


def already_registered(sid: str) -> bool:
    for f in THREADS.glob("cc_*.json"):
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if rec.get("claude_session") == sid:
            return True
    return False


def typed_name(transcript: str) -> str:
    """The name given to `/branch <name>`, if one was.

    `/branch` records the branch's title beside its transcript before it resumes,
    so it is already on disk by the time this runs. With no name it derives one
    and marks it with the same `(Branch)` suffix it uses for numbering, which is
    what tells a typed name from a derived one -- only a typed name is a label.
    """
    if not transcript:
        return ""
    try:
        rec = json.loads(
            (Path(transcript).with_suffix("") / "custom-title.json").read_text())
    except (OSError, ValueError):
        return ""
    title = re.sub(r"\s+", " ", str(rec.get("customTitle") or "")).strip()
    return "" if SUFFIX.search(title) else title[:MAX_LABEL]


def branch_label(label: str) -> str:
    """"cockpit" -> "cockpit (branch)" -> "cockpit (branch 2)"."""
    m = SUFFIX.search(label)
    if not m:
        return f"{label} (branch)" if label else "branch"
    n = int(m.group(1) or 1) + 1
    return f"{label[:m.start()]} (branch {n})"


def write(path: Path, key: str, value) -> None:
    """Field-at-a-time, the way cc-host.sh writes, so a concurrent refresh
    rewriting recap or state cannot be clobbered by a whole-file write."""
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    rec[key] = value
    tmp = path.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(rec))
    tmp.replace(path)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    new_sid = payload.get("session_id") or ""
    if not new_sid:
        return 0

    tid = tmux_session()
    if not tid:
        return 0
    row = THREADS / f"{tid}.json"
    if not row.is_file():
        return 0
    try:
        rec = json.loads(row.read_text())
    except (OSError, ValueError):
        return 0

    old_sid = parent_session(
        payload.get("transcript_path") or "", rec.get("claude_session") or "", new_sid)
    if not old_sid or old_sid == new_sid:
        return 0

    # The pane is the branch now. Recorded here rather than left to the next
    # refresh, so the parent cannot be registered while the row still claims it.
    write(row, "claude_session", new_sid)
    # `/branch cars` means the tangent is about cars, so that is its label and the
    # original keeps the name it had. Unnamed, there is nothing to call it but
    # what it came from.
    write(row, "label",
          typed_name(payload.get("transcript_path") or "")
          or branch_label(rec.get("label") or ""))

    if already_registered(old_sid):
        return 0
    try:
        subprocess.run(
            [str(HOST), "register", old_sid,
             rec.get("topic") or "", rec.get("label") or old_sid],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
