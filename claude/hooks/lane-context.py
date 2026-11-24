#!/usr/bin/env python3
"""Tell the lanes main chat which lane the user has selected.

Wired to UserPromptSubmit and SessionStart. Only the main chat that
lanes-session.sh starts (CC_LANES_MAIN=1) reacts; every other conversation
exits silently so the hook costs them nothing.

  UserPromptSubmit      the selected lane's brief (`lane show`) and where its
                        original transcript is, so the chat can work on it
                        without the user retyping which lane. Sent in full once
                        per lane change; after that a one-line reminder.
  SessionStart          how to keep the lanes true (FILING_RULES), plus
                        `lane digest` after a compact or resume, when the
                        model has lost the lane list.

Runs on every prompt, so it must stay under ~300 ms and must never print
anything but the JSON below. Any failure exits 0 with no output.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

DIGEST_LINES = 40
DIGEST_TIMEOUT = 2.0
DIGEST_SOURCES = ("compact", "resume")
RULES_SOURCES = ("startup", "clear", "compact", "resume")
SWITCH_RULE = (
    "If their message is about that lane, work on it. If it is clearly about different work, "
    "don't force it in: `lane select` the matching lane or `lane new` one, and say so in one "
    "line. If you can't tell, ask in that line before doing anything."
)
FILING_RULES = """Helm lanes: you are the main chat; the list beside you is the user's map of their work. Keep it true:
- One lane per deliverable: a fix, feature, investigation, request from a person, or review. Follow-ups, fixes and reviews of that same deliverable stay in its lane, even across days.
- New lane when the work has a different outcome, repo or ticket, or a different person asking: `lane new "<3-6 word title>" --goal "<who wants what, one line>" --topic <slug>`. Check `lane list` first so you don't make a duplicate.
- Topic = the project or product (billing, mobile, docs...). Reuse one from `lane topics known`; make a new slug only for a genuinely new project.
- The selected lane is a default, not an assumption: a message clearly about other work goes to its own lane (switch with `lane select`, or `lane new`), never into the selected one.
- Say where you filed it in one line ("Filed under billing: new lane 'Invoice export fix'"), so the user can correct you. If it's unclear whether work is new, ask in that same line.
- Start every workflow's meta description with its lane id in brackets, e.g. "[t_60793b] Check invoice export"; that files it under the lane whatever is selected. Fix a miss with `lane link <workflow> <lane>`.
- A message touching several lanes: name each lane and handle each part on its own, update each card by id, make any new lane with `lane new --no-select`, leave the user's selection where it is, and end with one short line per lane saying what changed.
- Keep the card current: `lane brief <id> --recap "<where it stands>"` after each step, `lane ask <id> "<question>"` when you need the user, `lane done <id>` when it's finished."""
BRIEF_TIMEOUT = 2.0


def state_dir() -> Path:
    return Path(os.environ.get("LANES_STATE_DIR") or Path.home() / ".claude" / "helm")


def db_path() -> Path:
    return Path(os.environ.get("LANES_DB") or state_dir() / "helm.db")


def lane_bin() -> Path:
    default = Path.home() / ".tmux" / "scripts" / "lanes" / "lane"
    return Path(os.environ.get("LANES_BIN") or default)


def remember_main_session(session_id: str) -> None:
    """The launcher resumes this id, so the main chat survives a restart."""
    path = db_path()
    if not session_id or not path.parent.is_dir():
        return
    try:
        con = sqlite3.connect(str(path), timeout=0.2)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
            con.execute(
                "INSERT INTO meta (k, v) VALUES ('main_session_id', ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (session_id,),
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        return


def read_selection() -> Optional[dict]:
    try:
        data = json.loads((state_dir() / "selected-lane").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("id"):
        return None
    return data


def lane_brief(lane_id: str) -> str:
    try:
        proc = subprocess.run([str(lane_bin()), "show", lane_id], capture_output=True,
                              text=True, timeout=BRIEF_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def transcript_of(lane_id: str) -> str:
    """The lane's own conversation on disk, for detail the brief leaves out."""
    try:
        con = sqlite3.connect(str(db_path()), timeout=0.2)
        try:
            row = con.execute("SELECT source FROM lanes WHERE id=?", (lane_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    sid = row[0] if row and row[0] else ""
    if not sid or "/" in sid:
        return ""
    projects = Path(os.environ.get("CLAUDE_PROJECTS") or Path.home() / ".claude" / "projects")
    hits = sorted(projects.glob(f"*/{sid}.jsonl"))
    return str(hits[0]) if hits else ""


def injected_path() -> Path:
    return state_dir() / "lane-injected"


def already_briefed(key: str) -> bool:
    try:
        return injected_path().read_text() == key
    except OSError:
        return False


def mark_briefed(key: str) -> None:
    try:
        injected_path().write_text(key)
    except OSError:
        pass


def mark_busy(transcript: str) -> None:
    """The lanes list spins the selected lane while the main chat is mid-turn.
    The Stop hook (cc-recap-trigger.sh) removes this; the list also stops
    trusting it once the transcript goes quiet, since an interrupt skips Stop."""
    try:
        (state_dir() / "main-busy").write_text(json.dumps({"transcript": transcript}))
    except OSError:
        pass


def selection_context(sel: dict) -> str:
    if sel.get("kind") == "chat":
        return ""
    lane_id, title = sel["id"], sel.get("title", "")
    brief = lane_brief(lane_id)
    key = lane_id + "\n" + brief
    if brief and already_briefed(key):
        return (f'Helm: lane {lane_id} "{title}" is still selected (brief given earlier). '
                + SWITCH_RULE)
    lines = [
        f'Helm: the user has lane {lane_id} "{title}" selected. Do the work here, in this '
        "chat. " + SWITCH_RULE,
    ]
    if brief:
        lines += ["", brief]
    else:
        lines.append(f"Run `lane show {lane_id}` for its brief.")
    transcript = transcript_of(lane_id)
    if transcript:
        lines += ["", f"Its original conversation (read-only, for detail): {transcript}"]
    lines += ["", f"When a step lands, update it: lane brief {lane_id} --recap '...'"]
    if brief:
        mark_briefed(key)
    return "\n".join(lines)


def digest_context() -> str:
    try:
        proc = subprocess.run(
            [str(lane_bin()), "digest"],
            capture_output=True,
            text=True,
            timeout=DIGEST_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        return ""
    return "Helm lanes:\n" + "\n".join(lines[:DIGEST_LINES])


def context_for(event: str, payload: dict) -> str:
    if event == "UserPromptSubmit":
        sel = read_selection()
        return selection_context(sel) if sel else ""
    if event == "SessionStart" and payload.get("source") in RULES_SOURCES:
        # The earlier brief may be gone from context; send it again next prompt.
        injected_path().unlink(missing_ok=True)
        parts = [FILING_RULES]
        if payload.get("source") in DIGEST_SOURCES:
            parts.append(digest_context())
        return "\n\n".join(p for p in parts if p)
    return ""


def main() -> None:
    # One-shot `claude -p` children inherit CC_LANES_MAIN; they must not claim to be the chat.
    if os.environ.get("CC_LANES_MAIN") != "1" or os.environ.get("CC_RECAP_CHILD"):
        return
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        return
    remember_main_session(str(payload.get("session_id") or ""))
    event = str(payload.get("hook_event_name") or "")
    if event == "UserPromptSubmit":
        mark_busy(str(payload.get("transcript_path") or ""))
    text = context_for(event, payload)
    if not text:
        return
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
