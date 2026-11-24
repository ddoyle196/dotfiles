#!/usr/bin/env python3
"""Group lanes into a few durable topics so nobody has to name them by hand.

  topics.py known     topic names in use, one per line (recap hook feeds these
                      to the model so new conversations reuse them)
  topics.py suggest   one Haiku call over all live conversations; saves a
                      proposal and prints a before -> after table
  topics.py apply     apply the saved proposal; topics the user pinned stay put

The proposal is a file so `suggest` can be reviewed (or edited) before `apply`.
Nothing here touches the helm registry: the lanes db is the only thing changed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

MODEL = "claude-haiku-4-5-20251001"
RECAP_CHARS = 160
SLUG_CHARS = 40
MODEL_TIMEOUT = 180
FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.DOTALL)


class ProposalError(ValueError):
    pass


def state_dir() -> Path:
    return Path(os.environ.get("LANES_STATE_DIR") or Path.home() / ".claude" / "helm")


def proposal_path() -> Path:
    return state_dir() / "topics-proposal.json"


def slugify(raw: str) -> str:
    text = re.sub(r"^\s*new:\s*", "", str(raw), flags=re.IGNORECASE).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:SLUG_CHARS].strip("-")


def known_topics(lanes: List[dict], declared: List[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for name in list(declared) + [ln.get("topic") or "" for ln in lanes]:
        slug = slugify(name)
        if slug:
            seen.setdefault(slug)
    return list(seen)


def declared_topics() -> List[str]:
    try:
        data = json.loads((state_dir() / "topics.json").read_text())
    except (OSError, ValueError):
        return []
    return [t for t in data if isinstance(t, str)] if isinstance(data, list) else []


def recap_of(lane: dict) -> str:
    trail = lane.get("trail") or []
    for ev in reversed(trail):
        if ev.get("kind") == "checkpoint":
            return str(ev.get("title") or "")
    return str(lane.get("status_detail") or "")


def conversation_rows(lanes: List[dict]) -> List[dict]:
    return [
        {
            "id": ln["id"],
            "title": ln.get("title") or "",
            "topic": ln.get("topic") or "",
            "recap": recap_of(ln)[:RECAP_CHARS],
        }
        for ln in lanes
        if ln.get("kind") == "conversation" and not ln.get("archived_at")
    ]


def build_prompt(rows: List[dict]) -> str:
    return (
        "Below are work conversations, one JSON object per line: id, title, current "
        "topic, last recap.\n\n"
        + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        + "\n\nGive each conversation the project or product it is about, so related work "
        "groups together. A topic is a durable project (an app, a system, an initiative), "
        "never a kind of activity. Keep a project's topic even if it has only one "
        "conversation. Merge only true duplicates and spelling variants, such as "
        '"pr reviews" and "pr-review", or a renamed project. Reviewing a PR belongs to '
        "the project the PR is in. Use \"personal\" only for things unrelated to any work "
        "project (devices, errands, career). Prefer the conversation's current topic when "
        "it already names its project. Topic names are short lowercase slugs "
        "(a-z, 0-9, hyphens).\n\n"
        "Reply with ONLY a JSON object, no prose and no code fence:\n"
        '{"mapping": {"<id>": "<topic>", ...}}\n'
        "Every id above must appear exactly once."
    )


def parse_proposal(text: str, valid_ids: List[str]) -> Dict[str, str]:
    body = text.strip()
    fenced = FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ProposalError(f"model reply is not JSON: {exc}") from exc
    mapping = data.get("mapping") if isinstance(data, dict) else None
    if not isinstance(mapping, dict):
        raise ProposalError('reply has no "mapping" object')
    allowed = set(valid_ids)
    out: Dict[str, str] = {}
    for lane_id, topic in mapping.items():
        if not isinstance(topic, str):
            raise ProposalError(f"topic for {lane_id} is not a string")
        slug = slugify(topic)
        if lane_id in allowed and slug:
            out[lane_id] = slug
    if not out:
        raise ProposalError("mapping covers none of the lanes")
    return out


def call_model(prompt: str) -> str:
    env = dict(os.environ, CC_RECAP_CHILD="1")
    env.pop("CC_LANES_MAIN", None)
    proc = subprocess.run(
        ["claude", "-p", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
         "--max-turns", "1", "--model", MODEL],
        input=prompt, capture_output=True, text=True, env=env, timeout=MODEL_TIMEOUT,
    )
    if proc.returncode != 0:
        raise ProposalError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def changes(rows: List[dict], mapping: Dict[str, str], pinned: set) -> List[dict]:
    by_id = {r["id"]: r for r in rows}
    out = []
    for lane_id, after in mapping.items():
        row = by_id.get(lane_id)
        if row is None or lane_id in pinned or slugify(row["topic"]) == after:
            continue
        out.append({"id": lane_id, "title": row["title"], "before": row["topic"], "after": after})
    return out


def format_table(items: List[dict]) -> str:
    if not items:
        return "No topic changes proposed."
    lines = [f'{i["id"]}  {i["title"][:34]:<34}  {i["before"] or "-"} -> {i["after"]}'
             for i in sorted(items, key=lambda i: (i["after"], i["title"]))]
    topics = len({i["after"] for i in items})
    lines.append(f"{len(items)} lanes change across {topics} topics. Run: lane topics apply")
    return "\n".join(lines)


def pinned_ids(lanes: List[dict]) -> set:
    return {ln["id"] for ln in lanes if ln.get("topic_source") == "you"}


def suggest(model: Callable[[str], str] = call_model) -> str:
    import lanedb
    con = lanedb.connect()
    lanes = lanedb.lanes(con)
    rows = conversation_rows(lanes)
    if not rows:
        return "No conversations to group."
    mapping = parse_proposal(model(build_prompt(rows)), [r["id"] for r in rows])
    items = changes(rows, mapping, pinned_ids(lanes))
    path = proposal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mapping": {i["id"]: i["after"] for i in items}}, indent=1))
    tmp.replace(path)
    return format_table(items)


def apply() -> str:
    import lanedb
    try:
        saved = json.loads(proposal_path().read_text())["mapping"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ProposalError("no saved proposal; run: lane topics suggest")
    con = lanedb.connect()
    pinned = pinned_ids(lanedb.lanes(con))
    applied = 0
    for lane_id, topic in saved.items():
        if lane_id in pinned or lanedb.lane(con, lane_id) is None:
            continue
        lanedb.set_topic(con, lane_id, slugify(topic), source="ai")
        applied += 1
    return f"Applied {applied} topics ({len(saved) - applied} skipped)."


def known() -> str:
    try:
        import lanedb
        lanes = lanedb.lanes(lanedb.connect())
    except Exception:
        lanes = []
    return "\n".join(known_topics(lanes, declared_topics()))


COMMANDS: Dict[str, Callable[[], str]] = {"known": known, "suggest": suggest, "apply": apply}


def main(argv: List[str]) -> int:
    cmd = COMMANDS.get(argv[0]) if argv else None
    if cmd is None:
        print("usage: topics.py known|suggest|apply", file=sys.stderr)
        return 2
    try:
        out = cmd()
    except (ProposalError, subprocess.SubprocessError, OSError) as exc:
        print(f"topics: {exc}", file=sys.stderr)
        return 1
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
