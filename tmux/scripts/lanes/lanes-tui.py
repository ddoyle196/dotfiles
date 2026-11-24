#!/usr/bin/env python3
"""Lanes TUI: every conversation and workflow as a lane, plus a read-only card.

Reads lanes through lanedb.py (the db is filled by `lane sync`). The only writes
are the ones a key asks for: archive, pin a topic, write selected-lane. Keeps
the layout, grouping and text in pure functions so tests can pin them without a
terminal; the curses shell at the bottom only draws and routes keys.
"""
from __future__ import annotations

import curses
import json
import os
import shlex
import subprocess
import sys
import textwrap
import threading
import time
import zlib
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import lanedb  # noqa: E402

Segment = Tuple[str, str]
Line = List[Segment]

SYNC_EVERY = 15
CHAT_ID = "chat"
MISC_TOPIC = "unsorted"

# glyph, label, style. Order is also the sort order inside a topic.
STATUS_CELLS = {
    "you": ("◇", "needs you", "yellow"),
    "stalled": ("!", "stalled", "red"),
    "working": ("▸", "working", "green"),
    "person": ("◷", "waiting on", "magenta"),
    "review": ("◎", "review", "blue"),
    "ci": ("⇡", "ci", "cyan"),
    "ready": ("›", "ready", "plain"),
    "idle": ("·", "idle", "dim"),
    "done": ("✓", "done", "dim"),
}
STATUS_ORDER = ["stalled", "working", "person", "review", "ci", "ready", "idle", "done"]
ACTIVE_STATUSES = ("you", "working", "person", "review", "ci", "stalled")
RUNNING_STATUSES = ("working", "stalled")
CHILD_DONE_FOR = 2 * 3600
# Lanes in these statuses are pulled out of their topic into a section at the top.
TOP_STATUSES = ("you",) + RUNNING_STATUSES

TOPIC_COLORS = 16


# 256-colour picks that stay apart on a dark background; 8-colour terminals
# cycle the basic six instead. tmux paints the main chat's lane chip from the
# same list, so a lane's row and the chat working on it share a colour.
TOPIC_256 = (75, 213, 114, 215, 141, 80, 222, 168, 149, 111,
             203, 43, 180, 177, 117, 186)
_topic_slots: Dict[str, int] = {}


def assign_topic_colors(topics: Iterable[str]) -> None:
    """`topics` busiest first. Each starts at its hash slot and moves to the
    next free one, so the first TOPIC_COLORS never share a colour; quieter
    topics past that reuse their hash slot."""
    _topic_slots.clear()
    taken: Set[int] = set()
    for name in dict.fromkeys(topics):
        slot = zlib.crc32(name.encode()) % TOPIC_COLORS
        for step in range(TOPIC_COLORS):
            if (slot + step) % TOPIC_COLORS not in taken:
                slot = (slot + step) % TOPIC_COLORS
                break
        taken.add(slot)
        _topic_slots[name] = slot


def topics_by_activity(lanes: Sequence[Dict]) -> List[str]:
    """Topics with live (not stale) lanes first, busiest first, then the rest."""
    score: Dict[str, Tuple[int, int]] = {}
    for l in lanes:
        if l.get("archived_at"):
            continue
        name = l.get("topic") or MISC_TOPIC
        live, count = score.get(name, (0, 0))
        score[name] = (live + (0 if is_stale(l) else 1), count + 1)
    return sorted(score, key=lambda t: (-score[t][0], -score[t][1], t))


def topic_colour(topic: str) -> str:
    return "colour%d" % TOPIC_256[int(topic_style(topic)[len("topic"):])]


def topic_style(topic: str) -> str:
    name = topic or MISC_TOPIC
    slot = _topic_slots.get(name)
    if slot is None:
        slot = zlib.crc32(name.encode()) % TOPIC_COLORS
    return "topic%d" % slot


EVENT_GLYPHS = {
    "checkpoint": "●",
    "answer": "◆",
    "ask": "◇",
    "block": "⊘",
    "unblock": "○",
    "pr": "⑂",
    "phase": "▪",
    "done": "✓",
    "archive": "▫",
    "note": "·",
}
EVENT_STYLES = {"ask": "yellow", "block": "magenta", "pr": "cyan", "phase": "green"}

HELP_LINES = [
    "j / k      next / previous lane",
    "J / K      older / newer stop inside the lane",
    "n          next lane that needs you",
    "enter      talk about this lane in the main chat (it gets the lane's brief)",
    "o          open the lane's own conversation in the chat pane",
    "c          back to the main chat",
    "a          archive this lane",
    "A          archive every stale lane (asks first)",
    "t          set the topic (tab completes; pins it)",
    "s          show / hide stale lanes",
    "d          second line: summary text, or the dot trail",
    "/          filter by text (esc clears)",
    "r          sync now",
    "?          this help        q  quit",
    "",
    "Status (end of each lane's second line)",
    "  ◇ needs you   ▸ working   ◷ waiting on a person   ! stalled",
    "  ◎ PR in review   ⇡ CI running or failing   › ready for you to pick up",
    "  · idle   ✓ done",
    "",
    "Trail dots (d), oldest to newest; J / K steps through them",
    "  ● checkpoint (a summary after a turn)   ◇ question to you   ◆ answered",
    "  ⊘ blocked   ○ unblocked   ⑂ PR change   ▪ workflow phase   ✓ finished",
]


# ---------------------------------------------------------------- text helpers


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(s: str) -> int:
    return sum(char_width(c) for c in s)


def clip_text(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if text_width(s) <= width:
        return s
    out, used = [], 0
    for ch in s:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def fit(s: str, width: int) -> str:
    """Clip, then pad with spaces to exactly `width` display cells."""
    s = clip_text(s, width)
    return s + " " * max(0, width - text_width(s))


def line_width(line: Line) -> int:
    return sum(text_width(t) for t, _ in line)


def clip_line(line: Line, width: int) -> Line:
    out: Line = []
    used = 0
    for text, style in line:
        room = width - used
        if room <= 0:
            break
        piece = clip_text(text, room) if text_width(text) > room else text
        out.append((piece, style))
        used += text_width(piece)
    return out


def plain(line: Line) -> str:
    return "".join(t for t, _ in line)


def fmt_age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return "now"
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def fmt_clock(ts: int) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"


def fmt_stamp(ts: int, now: int) -> str:
    """Clock time for today, short date for older, so a row says when."""
    if not ts:
        return "--:--"
    if now - ts < 86400:
        return fmt_clock(ts)
    return time.strftime("%b %d", time.localtime(ts)).replace(" 0", "  ")


def wrap(text: str, width: int, indent: str = "") -> List[str]:
    if width <= 4:
        return [clip_text(text, width)]
    out: List[str] = []
    for para in str(text).splitlines() or [""]:
        out.extend(
            textwrap.wrap(
                para, width, initial_indent=indent, subsequent_indent=indent
            )
            or [""]
        )
    return out


# ------------------------------------------------------------ pure view model


def lane_topic(lane: Dict, by_id: Dict[str, Dict]) -> str:
    """A workflow without a topic follows the conversation that launched it."""
    topic = (lane.get("topic") or "").strip()
    if topic:
        return topic
    parent = by_id.get(lane.get("parent") or "")
    if parent and (parent.get("topic") or "").strip():
        return parent["topic"].strip()
    return MISC_TOPIC


def is_stale(lane: Dict) -> bool:
    return bool(lane.get("stale_since")) and lane.get("status") not in ACTIVE_STATUSES


def matches(lane: Dict, query: str, by_id: Dict[str, Dict]) -> bool:
    if not query:
        return True
    hay = " ".join(
        str(x or "")
        for x in (
            lane.get("title"),
            lane_topic(lane, by_id),
            lane.get("status"),
            lane.get("status_detail"),
            lane.get("kind"),
        )
    ).lower()
    return all(word in hay for word in query.lower().split())


def status_rank(lane: Dict) -> int:
    status = lane.get("status")
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)


@dataclass
class Section:
    key: str
    title: str
    lanes: List[Dict] = field(default_factory=list)
    collapsed: bool = False
    hint: str = ""
    total: int = 0


def build_sections(
    lanes: Sequence[Dict], show_stale: bool = False, query: str = "", now: Optional[int] = None
) -> List[Section]:
    """Chat pinned first, then Needs you, Working, one section per topic, then Stale."""
    now = int(now if now is not None else time.time())
    by_id = {l["id"]: l for l in lanes}
    live = [l for l in lanes if not l.get("archived_at") and matches(l, query, by_id)]

    sections: List[Section] = []
    chat = [l for l in live if l["id"] == CHAT_ID]
    if chat:
        sections.append(Section("chat", "", chat))

    rest = [l for l in live if l["id"] != CHAT_ID]
    stale = [l for l in rest if is_stale(l)]
    fresh = [l for l in rest if not is_stale(l)]

    # A workflow is drawn under the lane that launched it, wherever that lane
    # sits, so it never reads as an unrelated sibling.
    fresh_ids = {l["id"] for l in fresh}
    children: Dict[str, List[Dict]] = {}
    hidden: Set[str] = set()
    for l in fresh:
        parent = l.get("parent")
        if l.get("kind") == "workflow" and parent in fresh_ids and parent != CHAT_ID:
            # Old finished runs would bury the live ones; they stay in history.
            if l.get("status") == "done" and now - (l.get("last_activity") or 0) > CHILD_DONE_FOR:
                hidden.add(l["id"])
                continue
            children.setdefault(parent, []).append(dict(l, _child=True))
    nested = {c["id"] for kids in children.values() for c in kids} | hidden
    top = [l for l in fresh if l["id"] not in nested]
    by_rank = lambda l: (status_rank(l), -(l.get("last_activity") or 0))

    def with_children(ls: List[Dict]) -> List[Dict]:
        out: List[Dict] = []
        for l in ls:
            out.append(l)
            out.extend(sorted(children.get(l["id"], []), key=by_rank))
        return out

    needs = sorted((l for l in top if l.get("status") == "you"),
                   key=lambda l: l.get("last_activity") or 0)
    if needs:
        sections.append(Section("needs", "Needs you", with_children(needs),
                                hint="oldest first", total=len(needs)))

    busy_parents = {c["parent"] for kids in children.values() for c in kids
                    if c.get("status") in RUNNING_STATUSES}
    running = lambda l: l.get("status") in RUNNING_STATUSES or l["id"] in busy_parents
    working = sorted((l for l in top if l.get("status") != "you" and running(l)),
                     key=lambda l: -(l.get("last_activity") or 0))
    if working:
        sections.append(Section("working", "Working", with_children(working),
                                hint="running now", total=len(working)))

    topics: Dict[str, List[Dict]] = {}
    for l in top:
        if l.get("status") != "you" and not running(l):
            topics.setdefault(lane_topic(l, by_id), []).append(l)
    newest = lambda ls: max((l.get("last_activity") or 0) for l in ls)
    for name in sorted(topics, key=lambda t: (-newest(topics[t]), t)):
        ordered = with_children(sorted(topics[name], key=by_rank))
        sections.append(Section("topic:" + name, name, ordered, total=len(topics[name])))

    if stale:
        expanded = show_stale or bool(query)
        stale.sort(key=lambda l: -(l.get("last_activity") or 0))
        sections.append(
            Section(
                "stale",
                "Stale",
                stale if expanded else [],
                collapsed=not expanded,
                hint="s to hide" if expanded else "s to show",
                total=len(stale),
            )
        )
    return sections


def flat_ids(sections: Sequence[Section]) -> List[str]:
    return [l["id"] for s in sections for l in s.lanes]


def reselect(prev_order: Sequence[str], prev_id: Optional[str], new_order: Sequence[str]) -> Optional[str]:
    """Keep the selection by id; if the lane left the list, take its neighbour."""
    if not new_order:
        return None
    if prev_id in new_order:
        return prev_id
    if prev_id in prev_order:
        i = prev_order.index(prev_id)
        for step in range(len(prev_order)):
            for j in (i + step, i - step):
                if 0 <= j < len(prev_order) and prev_order[j] in new_order:
                    return prev_order[j]
    return new_order[0]


def step_selection(order: Sequence[str], current: Optional[str], delta: int) -> Optional[str]:
    if not order:
        return None
    if current not in order:
        return order[0]
    return order[max(0, min(len(order) - 1, order.index(current) + delta))]


def next_needing(order: Sequence[str], lanes_by_id: Dict[str, Dict], current: Optional[str]) -> Optional[str]:
    start = order.index(current) if current in order else -1
    for k in range(1, len(order) + 1):
        lid = order[(start + k) % len(order)]
        if lanes_by_id[lid].get("status") == "you":
            return lid
    return None


# ------------------------------------------------------------------ rendering


def status_age(lane: Dict, now: int) -> str:
    return fmt_age(now - (lane.get("last_activity") or now))


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPIN_TICK = 0.12
CHAT_QUIET = 90
_spinning: Set[str] = set()


def spin_glyph() -> str:
    return SPINNER[int(time.time() / SPIN_TICK) % len(SPINNER)]


def live_lanes(lanes: Sequence[Dict], selected: Optional[Dict], now: float) -> Set[str]:
    """Lanes with something moving right now: a running conversation, a
    running workflow and the lane it belongs to, and, while the main chat is
    mid-turn, the lane the user has selected plus the chat row itself."""
    live: Set[str] = set()
    for l in lanes:
        # A conversation reads "working" only while its own Claude is mid-turn.
        if l.get("kind") == "conversation" and l.get("status") == "working":
            live.add(l["id"])
            continue
        # Sync calls a workflow "working" while an agent has started with no
        # result (a long command writes nothing meanwhile) and "stalled" after
        # 20 silent minutes, so working is already the live signal.
        if l.get("kind") == "workflow" and l.get("status") == "working":
            live.add(l["id"])
            if l.get("parent"):
                live.add(l["parent"])
    if main_chat_busy(now):
        live.add(CHAT_ID)
        if selected and selected.get("id"):
            live.add(selected["id"])
    return live


def main_chat_busy(now: float) -> bool:
    try:
        with open(os.path.join(lanedb_helm_dir(), "main-busy"), encoding="utf-8") as fh:
            transcript = json.load(fh).get("transcript") or ""
        return now - os.path.getmtime(transcript) < CHAT_QUIET
    except (OSError, ValueError, AttributeError):
        return False


def status_cell(lane: Dict, now: int) -> Segment:
    status = lane.get("status") or "idle"
    glyph, label, style = STATUS_CELLS.get(status, STATUS_CELLS["idle"])
    if lane["id"] in _spinning:
        glyph = spin_glyph()
        if status not in ("you", "working", "stalled"):
            status, label, style = "working", "working", STATUS_CELLS["working"][2]
    detail = (lane.get("status_detail") or "").strip()
    if status == "you":
        body = "%s %s · %s" % (glyph, label, status_age(lane, now))
    elif status == "working":
        body = "%s %s%s" % (glyph, label, " · " + detail if detail else "")
    elif status in ("person", "review"):
        body = "%s %s%s" % (glyph, label, " " + detail if detail else "")
    elif status == "ci":
        body = "%s %s%s" % (glyph, label, " " + detail if detail else "")
    else:
        body = "%s %s" % (glyph, label)
    return ("[" + clip_text(body, 34) + "]", style)


def event_glyph(event: Dict) -> str:
    return EVENT_GLYPHS.get(event.get("kind"), "·")


def trail_line(
    lane: Dict, width: int, now: int, stop_pos: Optional[int] = None, indent: int = 2
) -> Line:
    """Glyph per recent event joined by ─, then the status cell.

    `stop_pos` is a position inside lane['trail'] (oldest first) to mark.
    The cell always survives; glyphs give way first on a narrow pane.
    """
    cell = status_cell(lane, now)
    cell_w = text_width(cell[0]) + 2
    room = max(0, width - indent - cell_w)
    events = list(lane.get("trail") or [])[-8:]
    base = len(lane.get("trail") or []) - len(events)
    parts: Line = [(" " * indent, "plain")]
    used = 0
    for i, ev in enumerate(events):
        need = 2 if i else 1
        if used + need > room:
            break
        if i:
            parts.append(("─", "dim"))
        style = EVENT_STYLES.get(ev.get("kind"), "plain")
        if stop_pos is not None and base + i == stop_pos:
            style += "+rev"
        parts.append((event_glyph(ev), style))
        used += need
    parts.append(("─ " if events else "", "dim"))
    parts.append(cell)
    return parts


def lane_name(lane: Dict) -> str:
    title = lane.get("title") or lane.get("id") or "?"
    kind = lane.get("kind")
    if kind == "workflow":
        return "wf " + title
    if kind == "chat":
        return "chat · main conversation"
    return title


def summary_text(lane: Dict) -> str:
    """The most useful sentence about a lane: its ask or blocker, else where it stands."""
    if lane.get("kind") == "workflow":
        return ""
    detail = (lane.get("status_detail") or "").strip()
    if lane.get("status") == "you" and detail:
        return detail
    return (lane.get("recap") or lane.get("goal") or "").strip()


def summary_line(lane: Dict, width: int, now: int) -> Line:
    # Needs-you lanes sit under a "Needs you" heading with their age on the
    # name line, so the label and age would only repeat: keep the glyph.
    if lane.get("status") == "you":
        glyph, _, style = STATUS_CELLS["you"]
        if lane["id"] in _spinning:
            glyph = spin_glyph()
        text = summary_text(lane) or "waiting on your answer"
        return [("  ", "plain"), (glyph, style), (" ", "plain"),
                (clip_text(text, max(1, width - 4)), "dim")]
    cell = status_cell(lane, now)
    room = width - text_width(cell[0]) - 3
    text = summary_text(lane)
    if room < 6 or not text:
        return [("  ", "plain"), cell]
    return [("  ", "plain"), cell, (" ", "plain"), (clip_text(text, room), "dim")]


def lane_lines(
    lane: Dict,
    width: int,
    now: int,
    selected: bool,
    events: Sequence[Dict] = (),
    stop_idx: int = 0,
    trails: bool = False,
    expanded: Optional[Sequence[Line]] = None,
) -> List[Line]:
    """Two lines per lane. The selected one opens in place: with `expanded`
    (its context) when given, else its last six stops."""
    age = "" if lane.get("kind") == "chat" else fmt_age(now - (lane.get("last_activity") or now))
    tstyle = topic_style(lane.get("topic") or MISC_TOPIC)
    marker = ("▌", tstyle + "+bold") if selected else ("▏", tstyle)
    child = bool(lane.get("_child"))
    branch = "└ " if child else ""
    # The top sections pull lanes out of their topic group, so the row carries it.
    pulled = lane.get("status") in TOP_STATUSES and not child
    topic = " · " + (lane.get("topic") or MISC_TOPIC) if pulled else ""
    name_room = max(4, width - 2 - text_width(age) - 1 - text_width(topic) - text_width(branch))
    name_style = "bold+rev" if selected else "bold"
    name = clip_text(lane_name(lane), name_room)
    pad = " " * max(0, name_room - text_width(name))
    head: Line = [
        marker,
        (" ", "plain"),
        (branch, "dim"),
        (name, name_style),
        (topic, tstyle),
        (pad + " " + age, "dim"),
    ]
    trail_len = len(lane.get("trail") or [])
    stop_pos = trail_len - 1 - stop_idx if selected and 0 <= stop_idx < trail_len else None
    if trails:
        body = trail_line(lane, width - 2, now, stop_pos, indent=0)
    elif selected and expanded is not None:
        body = [status_cell(lane, now)]
    else:
        body = summary_line(lane, width - 2, now)
    lead: Line = [marker, (" ", "plain")] + ([("  ", "plain")] if child else [])
    out = [clip_line(head, width), clip_line(lead + body, width)]
    if not selected:
        return out
    if expanded is not None:
        out += [clip_line([marker, (" ", "plain")] + line, width) for line in expanded]
        out.append([marker])
        return out
    for i, ev in enumerate(list(events)[:6]):
        glyph_style = EVENT_STYLES.get(ev.get("kind"), "plain")
        on = i == stop_idx
        row: Line = [
            ("▌" if on else " ", "cyan"),
            ("  ", "plain"),
            (fmt_stamp(ev.get("at") or 0, now).ljust(6), "dim"),
            (" ", "plain"),
            (event_glyph(ev), glyph_style),
            (" ", "plain"),
            (clip_text(ev.get("title") or "", max(4, width - 14)), "bold" if on else "plain"),
        ]
        out.append(clip_line(row, width))
    return out


@dataclass
class ListLayout:
    lines: List[Line]
    first_line: Dict[str, int]
    last_line: Dict[str, int]


def layout_list(
    sections: Sequence[Section],
    width: int,
    now: int,
    selected_id: Optional[str],
    events: Sequence[Dict] = (),
    stop_idx: int = 0,
    trails: bool = False,
    expanded: Optional[Sequence[Line]] = None,
) -> ListLayout:
    lines: List[Line] = []
    first: Dict[str, int] = {}
    last: Dict[str, int] = {}
    for sec in sections:
        if sec.title:
            if lines:
                lines.append([])
            label = "%s · %d" % (sec.title, sec.total)
            if sec.key == "needs":
                style = "yellow+bold"
            elif sec.key == "stale":
                style = "dim"
            else:
                style = topic_style(sec.title) + "+bold"
            head: Line = [(" " + label, style)]
            if sec.hint:
                head.append(("   " + sec.hint, "dim"))
            lines.append(clip_line(head, width))
        for lane in sec.lanes:
            is_sel = lane["id"] == selected_id
            block = lane_lines(lane, width, now, is_sel, events if is_sel else (), stop_idx, trails,
                               expanded if is_sel else None)
            first[lane["id"]] = len(lines)
            lines.extend(block)
            last[lane["id"]] = len(lines) - 1
    return ListLayout(lines, first, last)


def scroll_to_show(layout: ListLayout, selected_id: Optional[str], top: int, height: int) -> int:
    """Smallest scroll change that keeps the selected lane (with its stops) on screen."""
    if selected_id not in layout.first_line or height <= 0:
        return max(0, min(top, max(0, len(layout.lines) - height)))
    lo, hi = layout.first_line[selected_id], layout.last_line[selected_id]
    if lo < top:
        top = max(0, lo - 1)
    if hi >= top + height:
        # An open lane taller than the pane shows from its name, not its tail.
        top = lo if hi - lo + 1 > height else hi - height + 1
    return max(0, min(top, max(0, len(layout.lines) - height)))


# ----------------------------------------------------------------------- card


def journal_counts(path: Optional[str]) -> Optional[Tuple[int, int]]:
    """(started, finished) agents from a workflow journal; None when unreadable.

    Journal shape is undocumented, so every failure degrades to no counts.
    """
    if not path:
        return None
    try:
        if os.path.getsize(path) > 20_000_000:
            return None
        started, finished = set(), set()
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                agent = row.get("agentId") or row.get("key")
                if row.get("type") == "started" and agent:
                    started.add(agent)
                elif row.get("type") == "result" and agent:
                    finished.add(agent)
        return len(started), len(finished & started)
    except OSError:
        return None


def last_seen_of(lane_id: str) -> int:
    path = os.path.join(lanedb_helm_dir(), "threads", lane_id + ".json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(json.load(fh).get("last_seen") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def lanedb_helm_dir() -> str:
    return os.environ.get("LANES_STATE_DIR") or os.path.expanduser("~/.claude/helm")


def event_text(ev: Dict) -> str:
    """Sync clips the title to 90 chars and keeps the whole text as detail."""
    title = (ev.get("title") or "").rstrip("…").rstrip()
    detail = ev.get("detail") or ""
    if detail.startswith(title):
        return detail
    return (ev.get("title") or "") + ("\n" + detail if detail else "")


def now_box(lane: Dict, events: Sequence[Dict], counts: Optional[Tuple[int, int]]) -> Optional[Tuple[str, str]]:
    """Title and body of the NOW box: the one thing to know about this lane."""
    status = lane.get("status")
    detail = (lane.get("status_detail") or "").strip()
    newest_first = list(events)

    def latest(kind: str) -> Optional[Dict]:
        return next((e for e in newest_first if e.get("kind") == kind), None)

    if lane.get("kind") == "workflow":
        phase = detail or (latest("phase") or {}).get("title") or "no phase yet"
        body = "phase: " + phase
        if counts:
            body += "\nagents: %d started · %d finished" % counts
        return "NOW", body
    if status == "you":
        ask = latest("ask")
        question = detail or (ask or {}).get("detail") or (ask or {}).get("title")
        return "NEEDS YOU", question or "waiting on your answer"
    if status == "person":
        block = latest("block")
        return "BLOCKED", detail or (block or {}).get("title") or "waiting on someone"
    # WHERE IT STANDS above already says what is next; a box would repeat it,
    # or worse, fall back to the lane's opening "started:" line.
    if lane.get("recap"):
        return None
    cp = next((e for e in newest_first if e.get("kind") == "checkpoint"
               and not (e.get("title") or "").startswith("started: ")), None)
    if not cp:
        return "NOW", "nothing recorded yet"
    return "NOW", event_text(cp)


def picture_lines(lane: Dict, width: int) -> List[Line]:
    """Big picture (what this is for) then medium (where it stands); the NOW
    box below is the small picture."""
    if lane.get("kind") not in ("conversation", "task"):
        return []
    out: List[Line] = []
    for label, text in (("TASK", lane.get("goal")), ("WHERE IT STANDS", lane.get("recap"))):
        out.append([(" " + label, "dim")])
        paras = wrap(text or "not recorded yet", max(8, width - 2))
        out += [[("  " + p, "plain" if text else "dim")] for p in paras[:4]]
        out.append([])
    return out


def boxed(title: str, body: str, width: int, style: str) -> List[Line]:
    inner = max(8, width - 5)
    top = "┌ %s " % title
    out: List[Line] = [[(" " + top + "─" * max(0, width - 2 - text_width(top)) + "┐", style)]]
    for para in wrap(body, inner):
        out.append([(" │ ", style), (fit(para, inner), "plain"), (" │", style)])
    out.append([(" └" + "─" * max(0, width - 3) + "┘", style)])
    return out


def card_lines(
    lane: Optional[Dict],
    detail: Optional[Dict],
    width: int,
    now: int,
    stop_idx: int = 0,
    last_seen: int = 0,
    counts: Optional[Tuple[int, int]] = None,
    header: bool = True,
) -> List[Line]:
    """Read-only context: header, the big/medium picture, NOW box, selected
    stop, recent events. Inline under a list row, the row is the header."""
    if not lane:
        return [[("  nothing selected", "dim")]]
    events = list((detail or {}).get("events") or [])
    inner = max(10, width - 2)
    out: List[Line] = []
    if header:
        cell = status_cell(lane, now)
        title_room = max(6, inner - text_width(cell[0]) - 1)
        out.append([
            (" ", "plain"),
            (fit(lane.get("title") or lane["id"], title_room), "bold"),
            (" ", "plain"),
            cell,
        ])
        kind = lane.get("kind") or "conversation"
        meta = ["topic: " + (lane.get("topic") or MISC_TOPIC), kind]
        if lane.get("parent"):
            meta.append("from " + lane["parent"])
        out.append([(" " + clip_text(" · ".join(meta), inner), "dim")])

    prs = []
    for ev in events:
        if ev.get("kind") == "pr" and ev.get("title") not in prs:
            prs.append(ev["title"])
    if lane.get("status") in ("review", "ci") and lane.get("status_detail"):
        label = lane["status_detail"]
        if not any(label in p for p in prs):
            prs.insert(0, label)
    if prs:
        out.append([(" PRs: " + clip_text("  ".join(prs[:3]), inner - 6), "cyan")])
    if lane.get("status") == "person" and lane.get("status_detail"):
        out.append([(" waiting on: " + lane["status_detail"], "magenta")])
    if out:
        out.append([])
    out.extend(picture_lines(lane, inner))

    box = now_box(lane, events, counts)
    if box:
        style = "yellow" if lane.get("status") == "you" else "dim"
        out.extend(boxed(box[0], box[1], width - 1, style))

    if 0 < stop_idx < len(events):
        ev = events[stop_idx]
        head = "STOP · %s · %s" % (fmt_stamp(ev.get("at") or 0, now), ev.get("kind"))
        out.append([])
        out.extend(boxed(head, event_text(ev), width - 1, "cyan"))

    if out and out[-1]:
        out.append([])
    out.extend(recap_lines(lane, events, inner, now, last_seen))
    return out


def recap_lines(lane: Dict, events: Sequence[Dict], width: int, now: int, last_seen: int) -> List[Line]:
    if lane.get("kind") == "workflow":
        phases = [e for e in events if e.get("kind") in ("phase", "done")][:5]
        out: List[Line] = [[(" PHASES", "dim")]]
        out += [_recap_row(e, width, now) for e in phases] or [[("  none yet", "dim")]]
        return out
    fresh = [e for e in events if (e.get("at") or 0) > last_seen] if last_seen else list(events)
    heading = " SINCE YOU LOOKED (%s)" % fmt_clock(last_seen) if last_seen else " RECENT"
    shown = fresh[:5]
    out = [[(heading, "dim")]]
    if not shown:
        return out + [[("  nothing new", "dim")]]
    return out + [_recap_row(e, width, now) for e in shown]


def _recap_row(ev: Dict, width: int, now: int) -> Line:
    return clip_line(
        [
            ("  ", "plain"),
            (event_glyph(ev), EVENT_STYLES.get(ev.get("kind"), "plain")),
            (" ", "plain"),
            (fmt_stamp(ev.get("at") or 0, now).ljust(6), "dim"),
            (" ", "plain"),
            (ev.get("title") or "", "plain"),
        ],
        width,
    )


def split_layout(cols: int, rows: int) -> Dict[str, Tuple[int, int, int, int]]:
    """Regions as (y, x, h, w): one list column (the selected lane opens in
    place) over the footer."""
    return {"list": (0, 0, max(1, rows - 1), cols), "foot": (rows - 1, 0, 1, cols)}


# ------------------------------------------------------------- tmux + actions


class Tmux:
    """Targets panes by id from the options the launcher sets; never by index."""

    def __init__(self, run=None):
        self._run = run or self._subprocess

    @staticmethod
    def _subprocess(args: Sequence[str]) -> Tuple[int, str]:
        try:
            p = subprocess.run(
                ["tmux", *args], capture_output=True, text=True, timeout=5
            )
            return p.returncode, p.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return 1, ""

    def option(self, name: str) -> str:
        for flags in ("-qv", "-gqv"):
            rc, out = self._run(["show-options", flags, name])
            if rc == 0 and out:
                return out
        return ""

    def set_option(self, name: str, value: str) -> None:
        self._run(["set-option", "-q", name, value])

    def set_pane_option(self, pane: str, name: str, value: str) -> None:
        if value:
            self._run(["set-option", "-p", "-q", "-t", pane, name, value])
        else:
            self._run(["set-option", "-p", "-q", "-u", "-t", pane, name])

    def pane_exists(self, pane: str) -> bool:
        if not pane:
            return False
        rc, out = self._run(["list-panes", "-a", "-F", "#{pane_id}"])
        return rc == 0 and pane in out.split()

    def respawn(self, pane: str, command: str) -> bool:
        return self._run(["respawn-pane", "-k", "-t", pane, command])[0] == 0

    def focus(self, pane: str) -> None:
        self._run(["select-pane", "-t", pane])

    def swap(self, src: str, dst: str) -> bool:
        return self._run(["swap-pane", "-d", "-s", src, "-t", dst])[0] == 0

    def new_hidden(self, beside: str, command: str) -> str:
        """A pane in a background window of the same session, kept after it exits."""
        rc, session = self._run(["display-message", "-p", "-t", beside, "#{session_name}"])
        if rc != 0 or not session:
            return ""
        rc, pane = self._run(["new-window", "-d", "-n", "stage", "-t", "=%s:" % session,
                              "-P", "-F", "#{pane_id}", command])
        if rc != 0 or not pane:
            return ""
        # An attach that ends would otherwise close the pane and take the left
        # half of the layout with it while the main chat sits hidden.
        self._run(["set-option", "-p", "-t", pane, "remain-on-exit", "on"])
        return pane


def attach_command(lane_id: str, host: str) -> str:
    """Start the parked conversation if needed, then attach to it."""
    template = os.environ.get(
        "LANES_ATTACH_CMD", "unset TMUX; tmux attach -t '={id}' 2>/dev/null"
    )
    attach = template.replace("{id}", lane_id)
    start = "%s start %s >/dev/null 2>&1; " % (shlex.quote(host), shlex.quote(lane_id))
    after = "printf '\\n  conversation closed. press c in lanes for the chat\\n'"
    return "%s%s; %s" % (start, attach, after)


class Controller:
    """Turns Enter / c into pane changes. Selection is written first, always."""

    def __init__(self, tmux: Optional[Tmux] = None, host: Optional[str] = None):
        self.tmux = tmux or Tmux()
        self.host = host or os.environ.get("CC_HOST") or os.path.expanduser("~/.tmux/scripts/cc-host.sh")

    def activate(self, lane: Dict) -> str:
        """Enter: back to the one main chat, which now takes this lane as the
        subject of the next message (lane-context.py injects its brief)."""
        lanedb.write_selection(lane)
        if lane.get("kind") == "conversation":
            self._mark_seen(lane["id"])
        msg = self.show_chat()
        if lane.get("kind") == "chat" or msg != "main chat":
            return msg
        return "chat · about " + clip_text(lane.get("title") or lane["id"], 40)

    def open_original(self, lane: Dict) -> str:
        """o: the lane's own conversation, for talking to it directly."""
        lanedb.write_selection(lane)
        if lane.get("kind") == "conversation":
            return self.stage(lane)
        return self.show_chat()

    def stage(self, lane: Dict) -> str:
        """Show a real conversation where the chat sits, keeping the main chat alive.

        The main chat swaps into a background window rather than being replaced:
        killing it would also kill any workflow it is running.
        """
        chat = self.tmux.option("@lanes_chat_pane")
        if not self.tmux.pane_exists(chat):
            return "no chat pane: start with `helm lanes`"
        stage = self.tmux.option("@lanes_stage_pane")
        staged = self.tmux.option("@lanes_staged")
        if staged == lane["id"] and self.tmux.pane_exists(stage):
            self.tmux.focus(stage)
            return "staged " + clip_text(lane.get("title") or lane["id"], 30)
        cmd = attach_command(lane["id"], self.host)
        if self.tmux.pane_exists(stage):
            ok = self.tmux.respawn(stage, cmd)
        else:
            stage = self.tmux.new_hidden(chat, cmd)
            ok = bool(stage)
            if ok:
                self.tmux.set_option("@lanes_stage_pane", stage)
        if not ok:
            return "could not stage " + lane["id"]
        if not staged and not self.tmux.swap(stage, chat):
            return "could not stage " + lane["id"]
        self.tmux.set_option("@lanes_staged", lane["id"])
        self._mark_seen(lane["id"])
        self.tmux.focus(stage)
        return "staged " + clip_text(lane.get("title") or lane["id"], 30)

    def paint_chat(self, lane: Optional[Dict]) -> None:
        """The chip on the main chat's border names the lane it is working on,
        in that lane's topic colour (lanes-session.sh adds it to the format)."""
        chat = self.tmux.option("@lanes_chat_pane")
        if not self.tmux.pane_exists(chat):
            return
        if not lane or lane.get("kind") == "chat":
            label = colour = ""
        else:
            topic = lane.get("topic") or MISC_TOPIC
            label = "%s · %s" % (clip_text(lane.get("title") or lane["id"], 40), topic)
            colour = topic_colour(topic)
        self.tmux.set_pane_option(chat, "@lane_label", label)
        self.tmux.set_pane_option(chat, "@lane_color", colour)

    def show_chat(self) -> str:
        chat = self.tmux.option("@lanes_chat_pane")
        if not self.tmux.pane_exists(chat):
            return "no chat pane: start with `helm lanes`"
        stage = self.tmux.option("@lanes_stage_pane")
        if self.tmux.option("@lanes_staged") and self.tmux.pane_exists(stage):
            if not self.tmux.swap(chat, stage):
                return "could not restore the main chat"
        self.tmux.set_option("@lanes_staged", "")
        self.tmux.focus(chat)
        return "main chat"

    def _mark_seen(self, lane_id: str) -> None:
        spawn_quiet([self.host, "seen", lane_id])


def spawn_quiet(argv: Sequence[str]) -> None:
    try:
        subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


# ------------------------------------------------------------------ data + app


def chat_placeholder(now: int) -> Dict:
    return {
        "id": CHAT_ID, "kind": "chat", "title": "chat", "parent": None, "topic": None,
        "topic_source": None, "source": None, "created": now, "last_activity": now,
        "status": "idle", "status_detail": "", "stale_since": None, "archived_at": 0,
        "trail": [],
    }


def load_lanes(db_path: Optional[str], now: int) -> List[Dict]:
    con = lanedb.connect(db_path)
    try:
        rows = lanedb.lanes(con)
    finally:
        con.close()
    lanes = [dict(r) for r in rows]
    if not any(l["id"] == CHAT_ID for l in lanes):
        lanes.insert(0, chat_placeholder(now))
    return lanes


def load_detail(db_path: Optional[str], lane_id: str) -> Optional[Dict]:
    con = lanedb.connect(db_path)
    try:
        row = lanedb.lane(con, lane_id)
    finally:
        con.close()
    return dict(row) if row else None


def cc_host_archive(host: str, lane: Dict) -> bool:
    """Synchronous: a db-only archive is undone by the next sync."""
    if lane.get("kind") != "conversation":
        return True
    try:
        run = subprocess.run([host, "archive", lane["id"]], capture_output=True,
                             stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return run.returncode == 0


def archive_lanes(db_path: Optional[str], host: str, targets: Sequence[Dict]) -> int:
    con = lanedb.connect(db_path)
    done = 0
    try:
        for lane in targets:
            if lane["id"] == CHAT_ID:
                continue
            if not cc_host_archive(host, lane):
                continue
            lanedb.archive(con, lane["id"])
            done += 1
    finally:
        con.close()
    return done


class Syncer(threading.Thread):
    """Runs `lane sync` on an interval, then reloads, off the input loop."""

    def __init__(self, app: "App", cli: Optional[str]):
        super().__init__(daemon=True)
        self.app, self.cli = app, cli
        self.wake = threading.Event()
        self.stop = False

    def run(self) -> None:
        while not self.stop:
            self.app.set_syncing(True)
            summary = self.sync_once()
            self.app.reload(summary)
            self.app.set_syncing(False)
            self.wake.wait(SYNC_EVERY)
            self.wake.clear()

    def sync_once(self) -> str:
        if not self.cli or not os.path.exists(self.cli):
            return ""
        try:
            p = subprocess.run(
                [sys.executable, self.cli, "sync"],
                capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return "sync failed"
        return (p.stdout.strip().splitlines() or [""])[-1] if p.returncode == 0 else "sync failed"


class App:
    def __init__(self, db_path: Optional[str] = None, controller: Optional[Controller] = None):
        self.db_path = db_path
        self.ctl = controller or Controller()
        self.lock = threading.RLock()
        self.lanes: List[Dict] = []
        self.sections: List[Section] = []
        self.order: List[str] = []
        self.selected: Optional[str] = None
        self.stop_idx = 0
        self.top = 0
        self.show_stale = False
        self.trails = False
        self.spinning: Set[str] = set()
        self.spin_checked = 0.0
        self.painted: Optional[str] = "?"
        # Picks already on disk at startup are history, not a request to jump.
        self.picked_at = int((lanedb.read_selection() or {}).get("at") or 0)
        self.query = ""
        self.mode = "normal"
        self.buf = ""
        self.prompt_label = ""
        self.msg = ""
        self.summary = ""
        self.syncing = False
        self.dirty = True
        self.detail_cache: Dict[Tuple[str, int], Optional[Dict]] = {}
        self.counts_cache: Dict[Tuple[str, float], Optional[Tuple[int, int]]] = {}
        self.pending_archive: List[Dict] = []
        self.completions: List[str] = []
        self.comp_i = 0
        self.syncer: Optional[Syncer] = None
        self.reload("")

    # state ------------------------------------------------------------

    def now(self) -> int:
        return int(time.time())

    def set_syncing(self, on: bool) -> None:
        with self.lock:
            if self.syncing != on:
                self.syncing = on
                self.dirty = True

    def reload(self, summary: str) -> None:
        try:
            lanes = load_lanes(self.db_path, self.now())
        except Exception as exc:  # db mid-write or missing: keep the last view
            with self.lock:
                self.msg = "db: " + str(exc)[:60]
                self.dirty = True
            return
        picked = lanedb.read_selection()
        with self.lock:
            changed = lanes != self.lanes or (summary and summary != self.summary)
            self.lanes = lanes
            if summary:
                self.summary = summary
            self.detail_cache.clear()
            self.rebuild()
            self.follow_selection(picked)
            if changed:
                self.dirty = True

    def rebuild(self) -> None:
        prev_order, prev_sel = self.order, self.selected
        assign_topic_colors(topics_by_activity(self.lanes))
        self.sections = build_sections(self.lanes, self.show_stale, self.query)
        self.order = flat_ids(self.sections)
        self.selected = reselect(prev_order, prev_sel, self.order)
        if self.selected != prev_sel:
            self.stop_idx = 0

    def follow_selection(self, picked: Optional[Dict]) -> None:
        """Jump to a lane the chat selected (`lane new`, `lane select`); the
        list's own Enter writes the same file, so only newer picks move it."""
        at = int((picked or {}).get("at") or 0)
        if at <= self.picked_at:
            return
        self.picked_at = at
        if picked["id"] in self.order and picked["id"] != self.selected:
            self.selected, self.stop_idx = picked["id"], 0
            self.dirty = True

    def refresh_spinning(self) -> bool:
        """Re-check what is live about once a second (a few stats); returns
        whether anything spins, so the loop knows to keep animating."""
        now = time.time()
        if now - self.spin_checked >= 1.0:
            self.spin_checked = now
            picked = lanedb.read_selection()
            with self.lock:
                spinning = live_lanes(self.lanes, picked, now)
                if spinning != self.spinning:
                    self.spinning = spinning
                    self.dirty = True
                self.paint_chat(picked)
        _spinning.clear()
        _spinning.update(self.spinning)
        return bool(self.spinning)

    def paint_chat(self, picked: Optional[Dict]) -> None:
        """Repaint the chat's lane chip when the selection moves, whoever moved
        it: Enter here, or `lane new` / `lane select` from the chat."""
        lane = self.by_id().get((picked or {}).get("id") or "")
        key = "%s|%s|%s" % ((lane or {}).get("id"), (lane or {}).get("title"),
                            (lane or {}).get("topic"))
        if key == self.painted:
            return
        self.painted = key
        self.ctl.paint_chat(lane)

    def by_id(self) -> Dict[str, Dict]:
        return {l["id"]: l for l in self.lanes}

    def current(self) -> Optional[Dict]:
        return self.by_id().get(self.selected) if self.selected else None

    def detail(self) -> Optional[Dict]:
        lane = self.current()
        if not lane:
            return None
        key = (lane["id"], lane.get("last_activity") or 0)
        if key not in self.detail_cache:
            try:
                self.detail_cache[key] = load_detail(self.db_path, lane["id"])
            except Exception:
                self.detail_cache[key] = None
        return self.detail_cache[key]

    def counts(self, lane: Dict) -> Optional[Tuple[int, int]]:
        if lane.get("kind") != "workflow" or not lane.get("source"):
            return None
        try:
            stamp = os.path.getmtime(lane["source"])
        except OSError:
            return None
        key = (lane["source"], stamp)
        if key not in self.counts_cache:
            self.counts_cache[key] = journal_counts(lane["source"])
        return self.counts_cache[key]

    # keys -------------------------------------------------------------

    def handle(self, key: str) -> bool:
        """Returns False to quit."""
        with self.lock:
            return self._handle(key)

    def _handle(self, key: str) -> bool:
        self.dirty = True
        if self.mode == "help":
            self.mode = "normal"
            return True
        if self.mode == "prompt":
            return self.handle_prompt(key)
        if self.mode == "confirm":
            return self.handle_confirm(key)
        self.msg = ""
        return self.handle_normal(key)

    def handle_normal(self, key: str) -> bool:
        if key == "q":
            return False
        if key == "\x1b" and self.query:
            self.clear_filter()
            return True
        if key in ("j", "KEY_DOWN"):
            self.move(1)
        elif key in ("k", "KEY_UP"):
            self.move(-1)
        elif key == "J":
            self.move_stop(1)
        elif key == "K":
            self.move_stop(-1)
        elif key == "n":
            self.go_next_needing()
        elif key in ("\n", "KEY_ENTER", "\r"):
            self.activate()
        elif key == "o":
            self.open_original()
        elif key == "c":
            self.go_chat()
        elif key == "a":
            self.archive_current()
        elif key == "A":
            self.ask_archive_stale()
        elif key == "t":
            self.open_topic_prompt()
        elif key == "s":
            self.toggle_stale()
        elif key == "d":
            self.trails = not self.trails
        elif key == "/":
            self.open_prompt("filter", self.query)
        elif key == "r":
            self.msg = "syncing…"
            if self.syncer:
                self.syncer.wake.set()
            else:
                self.reload("")
        elif key == "?":
            self.mode = "help"
        return True

    def move(self, delta: int) -> None:
        self.selected = step_selection(self.order, self.selected, delta)
        self.stop_idx = 0

    def move_stop(self, delta: int) -> None:
        detail = self.detail()
        total = min(len((detail or {}).get("events") or []), 6)
        if total:
            self.stop_idx = max(0, min(total - 1, self.stop_idx + delta))

    def go_next_needing(self) -> None:
        target = next_needing(self.order, self.by_id(), self.selected)
        if target is None:
            self.msg = "nothing needs you"
            return
        self.selected, self.stop_idx = target, 0

    def activate(self) -> None:
        lane = self.current()
        if lane:
            self.msg = self.ctl.activate(lane)

    def open_original(self) -> None:
        lane = self.current()
        if lane:
            self.msg = self.ctl.open_original(lane)

    def go_chat(self) -> None:
        chat = self.by_id().get(CHAT_ID)
        if chat:
            if CHAT_ID in self.order:
                self.selected, self.stop_idx = CHAT_ID, 0
            self.msg = self.ctl.activate(chat)

    def toggle_stale(self) -> None:
        self.show_stale = not self.show_stale
        self.rebuild()

    def archive_current(self) -> None:
        lane = self.current()
        if not lane:
            return
        if lane["id"] == CHAT_ID:
            self.msg = "the main chat cannot be archived"
            return
        archive_lanes(self.db_path, self.ctl.host, [lane])
        self.msg = "archived " + clip_text(lane.get("title") or lane["id"], 30)
        self.reload("")

    def ask_archive_stale(self) -> None:
        stale = [l for l in self.lanes if is_stale(l) and not l.get("archived_at") and l["id"] != CHAT_ID]
        if not stale:
            self.msg = "no stale lanes"
            return
        self.pending_archive = stale
        self.mode = "confirm"
        self.prompt_label = "Archive %d stale lanes? y/n" % len(stale)

    def handle_confirm(self, key: str) -> bool:
        targets, self.pending_archive = self.pending_archive, []
        self.mode = "normal"
        if key == "y":
            n = archive_lanes(self.db_path, self.ctl.host, targets)
            self.msg = "archived %d stale" % n
            self.reload("")
        else:
            self.msg = "cancelled"
        return True

    # prompts ----------------------------------------------------------

    def open_prompt(self, kind: str, initial: str = "") -> None:
        self.mode = "prompt"
        self.prompt_kind = kind
        self.buf = initial
        self.prompt_label = {"filter": "/", "topic": "topic: "}[kind]
        self.completions, self.comp_i = [], 0

    def open_topic_prompt(self) -> None:
        lane = self.current()
        if not lane or lane["id"] == CHAT_ID:
            self.msg = "pick a conversation or workflow first"
            return
        self.open_prompt("topic", lane.get("topic") or "")

    def topic_names(self) -> List[str]:
        names = {(l.get("topic") or "").strip() for l in self.lanes}
        return sorted(n for n in names if n)

    def handle_prompt(self, key: str) -> bool:
        if key == "\x1b":
            self.mode = "normal"
            if self.prompt_kind == "filter":
                self.clear_filter()
            return True
        if key in ("\n", "\r", "KEY_ENTER"):
            self.mode = "normal"
            self.submit_prompt(self.buf.strip())
            return True
        if key in ("KEY_BACKSPACE", "\x7f", "\b"):
            self.buf = self.buf[:-1]
            self.completions = []
        elif key == "\t" and self.prompt_kind == "topic":
            self.complete()
        elif len(key) == 1 and key.isprintable():
            self.buf += key
            self.completions = []
        if self.prompt_kind == "filter":
            self.query = self.buf
            self.rebuild()
        return True

    def clear_filter(self) -> None:
        self.query = self.buf = ""
        self.rebuild()

    def complete(self) -> None:
        if not self.completions:
            stem = self.buf.lower()
            self.completions = [t for t in self.topic_names() if t.lower().startswith(stem)]
            self.comp_i = 0
        if self.completions:
            self.buf = self.completions[self.comp_i % len(self.completions)]
            self.comp_i += 1

    def submit_prompt(self, value: str) -> None:
        if self.prompt_kind == "filter":
            self.query = value
            self.rebuild()
            self.msg = "filter: " + value if value else "filter cleared"
            return
        lane = self.current()
        if not lane or not value:
            self.msg = "cancelled"
            return
        con = lanedb.connect(self.db_path)
        try:
            lanedb.set_topic(con, lane["id"], value, source="you")
        finally:
            con.close()
        self.msg = "topic: " + value
        self.reload("")


# ----------------------------------------------------------------- curses shell


class Screen:
    STYLE_COLORS = {"yellow": curses.COLOR_YELLOW, "green": curses.COLOR_GREEN,
                    "magenta": curses.COLOR_MAGENTA, "cyan": curses.COLOR_CYAN,
                    "red": curses.COLOR_RED, "blue": curses.COLOR_BLUE}
    TOPIC_256 = TOPIC_256
    TOPIC_8 = (curses.COLOR_BLUE, curses.COLOR_MAGENTA, curses.COLOR_GREEN,
               curses.COLOR_YELLOW, curses.COLOR_CYAN, curses.COLOR_RED)

    def __init__(self, stdscr):
        self.scr = stdscr
        self.pairs: Dict[str, int] = {}
        curses.curs_set(0)
        curses.use_default_colors()
        colors = dict(self.STYLE_COLORS)
        rich = curses.COLORS >= 256
        for i in range(TOPIC_COLORS):
            colors["topic%d" % i] = self.TOPIC_256[i] if rich else self.TOPIC_8[i % len(self.TOPIC_8)]
        for i, (name, color) in enumerate(colors.items(), start=1):
            curses.init_pair(i, color, -1)
            self.pairs[name] = i

    def attr(self, style: str) -> int:
        attr = 0
        for part in style.split("+"):
            if part in self.pairs:
                attr |= curses.color_pair(self.pairs[part])
            elif part == "bold":
                attr |= curses.A_BOLD
            elif part == "dim":
                attr |= curses.A_DIM
            elif part == "rev":
                attr |= curses.A_REVERSE
        return attr

    def put(self, y: int, x: int, line: Line, width: int) -> None:
        for text, style in clip_line(line, width):
            try:
                self.scr.addstr(y, x, text, self.attr(style))
            except curses.error:
                return
            x += text_width(text)

    def draw(self, app: App) -> None:
        rows, cols = self.scr.getmaxyx()
        self.scr.erase()
        with app.lock:
            if app.mode == "help":
                self.draw_help(rows, cols)
            else:
                self.draw_main(app, rows, cols)
            self.draw_footer(app, rows, cols)
        self.scr.refresh()

    def draw_help(self, rows: int, cols: int) -> None:
        self.put(1, 2, [("Lanes · keys", "bold")], cols - 2)
        for i, text in enumerate(HELP_LINES):
            if i + 3 < rows - 1:
                self.put(i + 3, 2, [(text, "plain")], cols - 3)
        if len(HELP_LINES) + 4 < rows - 1:
            self.put(len(HELP_LINES) + 4, 2, [("any key closes", "dim")], cols - 3)

    def draw_main(self, app: App, rows: int, cols: int) -> None:
        regions = split_layout(cols, rows)
        ly, lx, lh, lw = regions["list"]
        now = app.now()
        detail = app.detail()
        events = (detail or {}).get("events") or []
        lane = app.current()
        expanded = card_lines(
            lane, detail, lw - 4, now, app.stop_idx,
            last_seen_of(lane["id"]) if lane and lane.get("kind") == "conversation" else 0,
            app.counts(lane) if lane else None, header=False,
        ) if lane and lane.get("kind") != "chat" else None
        layout = layout_list(app.sections, lw - 1, now, app.selected, events, app.stop_idx,
                             app.trails, expanded)
        app.top = scroll_to_show(layout, app.selected, app.top, lh)
        if not layout.lines:
            msg = "no match" if app.query else "no lanes yet · r syncs"
            self.put(ly + 1, lx + 2, [(msg, "dim")], lw - 2)
        for i in range(lh):
            idx = app.top + i
            if idx < len(layout.lines):
                self.put(ly + i, lx, layout.lines[idx], lw - 1)

    def draw_footer(self, app: App, rows: int, cols: int) -> None:
        y = rows - 1
        if app.mode == "prompt":
            self.put(y, 0, [(" " + app.prompt_label + app.buf + "▏", "bold")], cols - 1)
        elif app.mode == "confirm":
            self.put(y, 0, [(" " + app.prompt_label, "yellow+bold")], cols - 1)
        else:
            tail = "syncing…" if app.syncing else app.summary
            left = " " + (app.msg or "j/k lane  n needs you  enter ask chat  o open convo  ? keys")
            gap = cols - 1 - text_width(left) - text_width(tail) - 1
            line: Line = [(left, "plain" if app.msg else "dim")]
            if gap > 1 and tail:
                line += [(" " * gap, "plain"), (tail, "dim")]
            self.put(y, 0, line, cols - 1)

    def key(self) -> Optional[str]:
        try:
            ch = self.scr.get_wch()
        except curses.error:
            return None
        if isinstance(ch, int):
            if ch == curses.KEY_RESIZE:
                return "RESIZE"
            return curses.keyname(ch).decode("ascii", "replace")
        return ch


def run(stdscr, app: App) -> None:
    # Too late for the ESCDELAY variable (initscr already ran); without this a
    # lone Esc waits a full second for the rest of an escape sequence.
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(25)
    screen = Screen(stdscr)
    stdscr.timeout(250)
    cli = os.environ.get("LANES_CLI") or os.path.join(HERE, "lane")
    app.syncer = Syncer(app, cli)
    app.syncer.start()
    minute = int(time.time() // 60)
    while True:
        animating = app.refresh_spinning()
        stdscr.timeout(int(SPIN_TICK * 1000) if animating else 250)
        if app.dirty or animating:
            app.dirty = False
            screen.draw(app)
        key = screen.key()
        if key is None:
            if int(time.time() // 60) != minute:
                minute = int(time.time() // 60)
                app.dirty = True
            continue
        if key == "RESIZE":
            app.dirty = True
            continue
        if not app.handle(key):
            break
    app.syncer.stop = True


def main() -> int:
    db_path = os.environ.get("LANES_DB") or None
    app = App(db_path)
    try:
        curses.wrapper(run, app)
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback

        log = os.path.join(lanedb_helm_dir(), "lanes-tui.crash.log")
        try:
            with open(log, "w", encoding="utf-8") as fh:
                traceback.print_exc(file=fh)
        except OSError:
            pass
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
