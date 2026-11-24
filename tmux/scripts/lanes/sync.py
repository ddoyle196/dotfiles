"""Fold the conversation registry and workflow journals into the lanes db.

Zero model calls. Conversations come from `cc-host.sh list` (the registry stays
authoritative); workflows come from journals Claude Code writes, which are
undocumented, so every read here is best-effort and a surprise skips that run.

Conversation state -> lane status (precedence is the order of derive_status):

  registry state / mark                    lane status   detail
  ---------------------------------------  ------------  ----------------------
  answer                                   you           question, else recap
  running                                  working       (none)
  blocked_on set, or waiting + suggestion  person        blocked_on / suggestion
  PR mark open, approved                   review        PR label
  PR mark fail, pending                    ci            PR label
  pickup                                   ready         recap
  done                                     done
  dead, empty, waiting w/o a name, other   idle
  `lane ask` question, until the next turn you (unless working)
  `lane ask --resolved`, until the next turn ready (an answered "you")
  PR mark draft, merged, closed, unknown   (no effect: nothing for the user to do)

Workflow journal -> lane status (Claude Code's run record, when readable,
overrides: killed/failed -> idle "stopped"/"failed", completed -> done):

  started > result and silent 20+ min      stalled       "N agents silent"
  written < 2 min ago, or started > result working       current phase
  every started has a result, idle 2+ min  done
  anything else                            idle
"""
import glob
import json
import os
import re
import subprocess
import time

import lanedb

STALE_AFTER = 7 * 86400
STALE_DONE_AFTER = 2 * 86400
STALE_EMPTY_AFTER = 86400
STALL_AFTER = 20 * 60
LIVE_WINDOW = 2 * 60
WORKFLOW_WINDOW = 14 * 86400
HOST_TIMEOUT = 60
RESULT_CHARS = 200

PROTECTED = ("you", "working", "person", "review", "ci", "stalled")
REVIEW_MARKS = ("open", "approved")
CI_MARKS = ("fail", "pending")
CHAT_ID = "chat"


def home_path(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def cc_host():
    return os.environ.get("CC_HOST") or home_path(".tmux", "scripts", "cc-host.sh")


def projects_dir():
    return os.environ.get("CLAUDE_PROJECTS") or home_path(".claude", "projects")


def session_index_dir():
    return os.environ.get("SESSION_INDEX") or home_path(".claude", "session-index")


# ---------------------------------------------------------------- status


def pr_status(prs):
    """(status, label) for the PR that most needs the user, or (None, None)."""
    for wanted, status in ((REVIEW_MARKS, "review"), (CI_MARKS, "ci")):
        for pr in prs or []:
            if pr.get("mark") in wanted:
                return status, pr.get("label", "")
    return None, None


def derive_status(row):
    """(status, detail) for one `cc-host.sh list` row."""
    state = row.get("state") or ""
    recap = row.get("recap") or ""
    if state == "answer":
        return "you", row.get("question") or recap
    if state == "running":
        return "working", ""
    waiting_on = row.get("blocked_on") or \
        (row.get("waiting_suggest") if state == "waiting" else "")
    if waiting_on:
        return "person", waiting_on
    pr, label = pr_status(row.get("prs"))
    if pr:
        return pr, label
    if state == "pickup":
        return "ready", recap
    if state == "done":
        return "done", ""
    return "idle", ""


def is_stale(status, last_activity, now, registry_state=""):
    if status in PROTECTED:
        return False
    age = now - (last_activity or 0)
    if age > STALE_AFTER:
        return True
    if status == "done" and age > STALE_DONE_AFTER:
        return True
    return registry_state == "empty" and age > STALE_EMPTY_AFTER


# ---------------------------------------------------------- conversations


def read_registry():
    """Rows from the host, or None when it is unavailable."""
    try:
        run = subprocess.run(
            [cc_host(), "list"], capture_output=True, text=True,
            timeout=HOST_TIMEOUT)
        rows = json.loads(run.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def read_json(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clean_topic(raw):
    """The recap prompt may answer `new: some-slug`; keep only the slug."""
    text = (raw or "").strip()
    if text.lower().startswith("new:"):
        text = text[4:]
    return text.strip().lower().replace(" ", "-")


def pick_topic(existing, registry_topic, suggested):
    """(topic, source). A pin or an applied AI topic outlives the registry's."""
    source = existing["topic_source"] if existing else None
    if source == "you":
        return existing["topic"], "you"
    # Only an unset or registry topic yields to a suggestion: letting every new
    # recap rewrite an 'ai' topic would undo `lane topics apply` on the next sync.
    if suggested and source not in ("ai",):
        return suggested, "ai"
    if source == "ai" and existing["topic"]:
        return existing["topic"], "ai"
    return (registry_topic or None), "registry"


def existing_lane(con, lane_id):
    return con.execute("SELECT * FROM lanes WHERE id=?", (lane_id,)).fetchone()


def sync_conversations(con, rows, now):
    empty_ids = set()
    for row in rows:
        lane_id = row["id"]
        prior = existing_lane(con, lane_id)
        sync_conversation(con, row, prior, now)
        if row.get("state") == "empty":
            empty_ids.add(lane_id)
    return empty_ids


def sync_conversation(con, row, prior, now):
    lane_id = row["id"]
    updated = int(row.get("updated_at") or row.get("created") or 0)
    status, detail = apply_resolved(
        con, lane_id, updated, *apply_ask(con, lane_id, updated, *derive_status(row)))
    index = (read_json(os.path.join(
        session_index_dir(), row["session_id"] + ".json")) or {}) \
        if row.get("session_id") else {}
    suggested = clean_topic(index.get("topic_suggest"))
    topic, source = pick_topic(prior, clean_topic(row.get("topic")), suggested)
    goal, goal_source = pick_goal(prior, index.get("goal"))
    recap, recap_at = pick_recap(prior, row.get("recap"), updated)
    archived_at = archived_stamp(row, prior, now)
    fields = dict(
        id=lane_id, kind="conversation",
        title=row.get("label") or row.get("topic") or lane_id,
        topic=topic, topic_source=source,
        source=row.get("session_id") or None,
        last_activity=updated, status=status, status_detail=detail,
        archived_at=archived_at,
        goal=goal, goal_source=goal_source, recap=recap, recap_at=recap_at,
    )
    if prior is None:
        fields["created"] = int(row.get("created") or updated)
    lanedb.upsert_lane(con, **fields)
    # A lane first seen now has history we can only date by its last update;
    # one we already knew changed since the last sync, so "now" is honest.
    event_at = now if prior else updated
    conversation_events(con, row, prior, updated, event_at, archived_at)


def ask_key(lane_id):
    return f"ask:{lane_id}"


def read_ask(con, lane_id):
    try:
        return json.loads(lanedb.get_meta(con, ask_key(lane_id)) or "null")
    except ValueError:
        return None


def apply_ask(con, lane_id, updated, status, detail):
    """An agent-recorded question holds the lane at "you" until the
    conversation itself speaks again; its own state then takes over."""
    ask = read_ask(con, lane_id)
    if not ask:
        return status, detail
    if updated > ask.get("since", 0):
        con.execute("DELETE FROM meta WHERE k=?", (ask_key(lane_id),))
        return status, detail
    if status == "working":
        return status, detail
    # Already asking on its own: the question set by hand is the better-worded one.
    return "you", ask.get("q") or detail


def pick_goal(prior, suggested):
    """A goal someone set by hand outranks the per-turn summary's guess."""
    if prior and prior["goal_source"] == "you":
        return prior["goal"], "you"
    suggested = " ".join(str(suggested or "").split())
    if suggested:
        return suggested, "summary"
    return (prior["goal"], prior["goal_source"]) if prior else (None, None)


def pick_recap(prior, registry_recap, updated):
    """The newer of the conversation's own recap and one written about it."""
    if prior and prior["recap"] and (prior["recap_at"] or 0) >= updated:
        return prior["recap"], prior["recap_at"]
    if registry_recap:
        return registry_recap, updated
    return (prior["recap"], prior["recap_at"]) if prior else (None, 0)


def resolved_key(lane_id):
    return f"resolved:{lane_id}"


def apply_resolved(con, lane_id, updated, status, detail):
    """`lane ask --resolved` answers a question the conversation itself asked,
    which no one can take back but that conversation; hold it off "you" until
    the conversation speaks again."""
    try:
        mark = json.loads(lanedb.get_meta(con, resolved_key(lane_id)) or "null")
    except ValueError:
        mark = None
    if not mark:
        return status, detail
    if updated > mark.get("since", 0):
        con.execute("DELETE FROM meta WHERE k=?", (resolved_key(lane_id),))
        return status, detail
    if status != "you":
        return status, detail
    return "ready", mark.get("note") or ""


def archived_stamp(row, prior, now):
    if not row.get("archived"):
        return 0
    if prior and prior["archived_at"]:
        return prior["archived_at"]
    return int(row.get("updated_at") or now)


def conversation_events(con, row, prior, updated, event_at, archived_at):
    lane_id = row["id"]
    recap = row.get("recap") or ""
    if recap:
        lanedb.add_event(
            con, lane_id, updated, "checkpoint", recap, recap,
            f"ck:{lane_id}:{updated}")
    if row.get("state") == "answer":
        question = row.get("question") or recap or "waiting on you"
        lanedb.add_event(
            con, lane_id, updated if not prior else event_at, "ask", question,
            None, f"ask:{lane_id}:{updated}")
    block_events(con, row, event_at)
    for pr in row.get("prs") or []:
        label, mark = pr.get("label"), pr.get("mark")
        lanedb.add_event(
            con, lane_id, event_at, "pr", f"{label} {mark}", None,
            f"pr:{lane_id}:{label}:{mark}")
    if archived_at:
        lanedb.add_event(
            con, lane_id, archived_at, "archive", "archived", None,
            f"ar:{lane_id}")


def block_events(con, row, event_at):
    lane_id = row["id"]
    key = f"blk:{lane_id}"
    before = lanedb.get_meta(con, key) or ""
    now_blocked = row.get("blocked_on") or ""
    if now_blocked == before:
        return
    since = int(row.get("blocked_since") or 0) or event_at
    if now_blocked:
        lanedb.add_event(
            con, lane_id, since, "block", f"blocked on {now_blocked}", None,
            f"bl:{lane_id}:{since}:{now_blocked}")
    else:
        lanedb.add_event(
            con, lane_id, event_at, "unblock", f"unblocked from {before}", None,
            f"ub:{lane_id}:{event_at}")
    lanedb.set_meta(con, key, now_blocked)


# -------------------------------------------------------------- workflows


def workflow_journals(now):
    pattern = os.path.join(
        projects_dir(), "*", "*", "subagents", "workflows", "*", "journal.jsonl")
    for path in glob.glob(pattern):
        try:
            if now - os.path.getmtime(path) <= WORKFLOW_WINDOW:
                yield path
        except OSError:
            continue


def parse_journal(path):
    """Counts and phases from a journal; result bodies are never kept."""
    started, resulted = {}, set()
    phases, last_result = [], ""
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            ident = entry.get("key") or entry.get("agentId")
            if not isinstance(ident, str):
                continue
            if kind == "started" and ident:
                started.setdefault(ident, entry)
                phase = entry.get("phase")
                if not isinstance(phase, str):
                    phase = None
                if phase and phase not in [p[0] for p in phases]:
                    phases.append((phase, entry.get("agentId")))
            elif kind == "result" and ident:
                resulted.add(ident)
                text = entry.get("result")
                if isinstance(text, str):
                    last_result = text[:RESULT_CHARS]
    current = None
    for entry in started.values():
        current = entry.get("phase") or current
    return {
        "started": len(started),
        "resulted": len(resulted & set(started)),
        "phases": phases,
        "phase": current,
        "last_result": last_result,
    }


STOPPED_RUNS = {"killed": "stopped", "failed": "failed", "error": "failed"}
RECORD_MAX_BYTES = 8 * 1024 * 1024


def run_record(journal_dir):
    """Claude Code's own record of a run (status, summary), written beside the
    session's scripts. Journals alone can't tell a stopped run from a busy one."""
    session_dir = os.path.dirname(os.path.dirname(os.path.dirname(journal_dir)))
    path = os.path.join(session_dir, "workflows",
                        os.path.basename(journal_dir) + ".json")
    try:
        if os.path.getsize(path) > RECORD_MAX_BYTES:
            return {}
        with open(path, errors="replace") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    return {k: record[k] for k in ("status", "summary")
            if isinstance(record.get(k), str)}


def workflow_status(info, idle, run_state=""):
    if run_state in STOPPED_RUNS:
        return "idle", STOPPED_RUNS[run_state]
    if run_state == "completed":
        return "done", ""
    outstanding = info["started"] > info["resulted"]
    if outstanding and idle >= STALL_AFTER:
        return "stalled", f"{info['started'] - info['resulted']} agents silent"
    if idle < LIVE_WINDOW or outstanding:
        return "working", info["phase"] or ""
    if info["started"] and idle >= LIVE_WINDOW:
        return "done", ""
    return "idle", ""


SCRIPT_DESCRIPTION = re.compile(
    r"""description\s*:\s*(?P<q>['"`])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)""")
SCRIPT_NAME = re.compile(r"""\bname\s*:\s*(?P<q>['"`])(?P<text>.*?)(?P=q)""")


def workflow_title(session, run_id):
    pattern = os.path.join(
        projects_dir(), "*", session, "workflows", "scripts", f"*-{run_id}.js")
    for path in glob.glob(pattern):
        try:
            with open(path, errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        for rx in (SCRIPT_DESCRIPTION, SCRIPT_NAME):
            hit = rx.search(head)
            if hit:
                return hit.group("text").replace("\\'", "'")
    return run_id


LANE_LABEL = re.compile(r"^\s*\[(?P<id>(?:t|cc)_[0-9a-f]{4,12})\]\s*")


def split_lane_label(title):
    """'[t_60793b] Check access' -> ('t_60793b', 'Check access'). The main chat
    labels a workflow's description so it files under that lane even when
    several lanes' workflows start at once."""
    hit = LANE_LABEL.match(title or "")
    if not hit:
        return None, title
    return hit.group("id"), title[hit.end():] or title


def workflow_parent(con, session, born):
    row = con.execute(
        "SELECT id FROM lanes WHERE kind='conversation' AND source=?",
        (session,)).fetchone()
    if row:
        return row["id"]
    if lanedb.get_meta(con, "main_session_id") == session:
        return selected_work_lane(con, born) or CHAT_ID
    return None


def selected_work_lane(con, born):
    """A main-chat workflow belongs to the lane the user had selected when it
    started; `lane link` fixes the rare miss."""
    sel = lanedb.read_selection()
    if not sel or sel.get("kind") not in ("task", "conversation"):
        return None
    if int(sel.get("at") or 0) > born:
        return None
    return sel["id"] if existing_lane(con, sel["id"]) else None


def parent_topic(con, parent):
    """Workflows are grouped with the conversation that launched them."""
    row = parent and existing_lane(con, parent)
    return (row["topic"] if row else None) or None


def agent_started_at(journal_dir, agent_id, fallback):
    if not agent_id:
        return fallback
    try:
        return int(os.path.getmtime(
            os.path.join(journal_dir, f"agent-{agent_id}.meta.json")))
    except OSError:
        return fallback


def birth_time(path, fallback):
    try:
        stat = os.stat(path)
    except OSError:
        return fallback
    return int(getattr(stat, "st_birthtime", fallback))


def sync_workflows(con, now):
    for path in workflow_journals(now):
        try:
            sync_workflow(con, path, now)
        except Exception:
            continue


def sync_workflow(con, path, now):
    journal_dir = os.path.dirname(path)
    run_id = os.path.basename(journal_dir)
    session = os.path.basename(os.path.dirname(os.path.dirname(
        os.path.dirname(journal_dir))))
    mtime = int(os.path.getmtime(path))
    info = parse_journal(path)
    record = run_record(journal_dir)
    status, detail = workflow_status(info, now - mtime, record.get("status", ""))
    prior = existing_lane(con, run_id)
    born = birth_time(path, mtime)
    label, title = split_lane_label(record.get("summary") or workflow_title(session, run_id))
    # A label names the lane outright. Without one the selection is a guess,
    # made once at first sight: the selection moves on long before the run ends.
    if label and existing_lane(con, label):
        parent = label
    elif prior and prior["parent"]:
        parent = prior["parent"]
    else:
        parent = workflow_parent(con, session, born)
    lanedb.upsert_lane(
        con, id=run_id, kind="workflow",
        title=title,
        parent=parent, topic=parent_topic(con, parent),
        topic_source="registry", source=path,
        created=born, last_activity=mtime,
        status=status, status_detail=detail)
    lanedb.set_meta(con, f"wf:{run_id}", json.dumps({
        "started": info["started"], "resulted": info["resulted"],
        "phase": info["phase"], "phases": [p[0] for p in info["phases"]],
    }))
    for phase, agent_id in info["phases"]:
        lanedb.add_event(
            con, run_id, agent_started_at(journal_dir, agent_id, mtime),
            "phase", phase, None, f"ph:{run_id}:{phase}")
    if status == "done":
        lanedb.add_event(
            con, run_id, mtime, "done",
            f"finished, {info['started']} agents",
            info["last_result"] or None, f"done:{run_id}")


# ------------------------------------------------------------------ tasks

TASK_HELD = ("you", "person", "done")


def sync_tasks(con):
    """Task lanes are made by the main chat (`lane new`), so nothing outside
    reports on them. They read as working while a workflow under them runs;
    needs-you, waiting and done are set by hand and held."""
    for row in con.execute(
            "SELECT id, status FROM lanes WHERE kind='task'"
            " AND COALESCE(archived_at, 0)=0").fetchall():
        if row["status"] in TASK_HELD:
            continue
        busy = con.execute(
            "SELECT 1 FROM lanes WHERE kind='workflow' AND parent=?"
            " AND status IN ('working', 'stalled')", (row["id"],)).fetchone()
        status = "working" if busy else "ready"
        if status != row["status"]:
            con.execute("UPDATE lanes SET status=? WHERE id=?", (status, row["id"]))


# ------------------------------------------------------------------- chat


def sync_chat(con, now):
    main = lanedb.get_meta(con, "main_session_id")
    activity = None
    if main:
        for path in glob.glob(os.path.join(projects_dir(), "*", f"{main}.jsonl")):
            try:
                activity = max(activity or 0, int(os.path.getmtime(path)))
            except OSError:
                continue
    fields = dict(id=CHAT_ID, kind="chat", title="chat", status="idle",
                  source=main)
    if existing_lane(con, CHAT_ID) is None:
        fields["created"] = now
    if activity:
        fields["last_activity"] = activity
    elif existing_lane(con, CHAT_ID) is None:
        fields["last_activity"] = now
    lanedb.upsert_lane(con, **fields)


# ------------------------------------------------------------------ stale


def retire_missing(con, present, now):
    """A conversation gone from the registry would otherwise keep its status forever."""
    for row in con.execute(
            "SELECT id FROM lanes WHERE kind='conversation'"
            " AND COALESCE(archived_at, 0)=0").fetchall():
        if row["id"] not in present:
            lanedb.archive(con, row["id"], now)


def mark_stale(con, now, empty_ids):
    for row in con.execute(
            "SELECT id, kind, status, last_activity, stale_since FROM lanes"
            " WHERE COALESCE(archived_at, 0)=0").fetchall():
        stale = row["kind"] != "chat" and is_stale(
            row["status"], row["last_activity"], now,
            "empty" if row["id"] in empty_ids else "")
        if stale and not row["stale_since"]:
            con.execute("UPDATE lanes SET stale_since=? WHERE id=?",
                        (now, row["id"]))
        elif not stale and row["stale_since"]:
            con.execute("UPDATE lanes SET stale_since=NULL WHERE id=?",
                        (row["id"],))


# ------------------------------------------------------------------- main


def sync(con, now=None):
    """Run one full pass; returns counts for the one-line summary."""
    now = int(now or time.time())
    rows = read_registry()
    with lanedb.transaction(con):
        empty_ids = set()
        if rows is not None:
            empty_ids = sync_conversations(con, rows, now)
        if rows is not None:
            retire_missing(con, {r["id"] for r in rows}, now)
        sync_chat(con, now)
        sync_workflows(con, now)
        sync_tasks(con)
        mark_stale(con, now, empty_ids)
        lanedb.set_meta(con, "last_sync", str(now))
    return summary(con)


def summary(con):
    row = con.execute(
        "SELECT COUNT(*) AS n,"
        " COALESCE(SUM(status='you'), 0) AS need_you,"
        " COALESCE(SUM(stale_since IS NOT NULL), 0) AS stale"
        " FROM lanes WHERE COALESCE(archived_at, 0)=0").fetchone()
    return {"lanes": row["n"], "need_you": row["need_you"],
            "stale": row["stale"]}


def summary_line(counts):
    return (f"{counts['lanes']} lanes · {counts['need_you']} need you"
            f" · {counts['stale']} stale")
